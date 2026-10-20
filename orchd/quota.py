"""Subscription quota observations only; no inference requests or credentials.

The default scope is the machine's local signed-in subscriptions. Separate homes
must be used for different accounts. Token totals and API budgets are never quota.
"""
import json
import contextlib
import datetime
import heapq
import math
import sqlite3
import time
import os
import re
import signal
import subprocess
import tempfile
import sys
import threading
from zoneinfo import ZoneInfo
from pathlib import Path

from . import store

# Observations are refreshed in the background every BACKGROUND_PERIOD seconds, so TTL must outlast a
# period plus probe latency (3x margin); the 7-day windows move slowly, so this stays accurate enough.
BACKGROUND_PERIOD = 300  # Default; ORCHD_QUOTA_REFRESH_SECONDS overrides, 0 disables.
TTL = 900
MAX_AGE = 1800
SONNET_CLI_TTL = 900  # Bounded CLI reads lack the continuous Codex refresh stream.
SONNET_CLOCK_SKEW = 2  # Tolerate small wall-clock/receipt races, never large future dates.
SONNET_PROBE_INTERVAL = 60  # Minimum seconds between auto-selection attempts, including failures.
DIAGNOSTIC_HISTORY = 10  # Completed attempts retained per provider.
REFRESH_INTERVAL = 30  # Refresh before observation TTL, leaving room for RPC latency.


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Cache:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else store.home() / 'quota.db'

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=2)
        con.execute('CREATE TABLE IF NOT EXISTS buckets (provider TEXT, id TEXT, observed REAL, '
                    'source TEXT, windows TEXT, PRIMARY KEY(provider,id))')
        con.execute('CREATE TABLE IF NOT EXISTS refresh_diagnostics (provider TEXT PRIMARY KEY, data TEXT)')
        return con

    def record_attempt(self, provider, started, duration, error):
        with contextlib.closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT data FROM refresh_diagnostics WHERE provider=?', (provider,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            entry = dict(attempt_at=started, duration_seconds=duration, error=error)
            data = dict(last_attempt_at=started,
                        last_success_at=started if error is None else previous.get('last_success_at'),
                        duration_seconds=duration, error=error,
                        history=(previous.get('history', []) + [entry])[-DIAGNOSTIC_HISTORY:])
            con.execute('INSERT INTO refresh_diagnostics VALUES (?,?) ON CONFLICT(provider) '
                        'DO UPDATE SET data=excluded.data', (provider, json.dumps(data)))

    def put(self, provider, bucket, windows, source, now):
        with contextlib.closing(self.connect()) as con, con:
            con.execute('INSERT INTO buckets VALUES (?,?,?,?,?) ON CONFLICT(provider,id) DO UPDATE SET '
                        'observed=excluded.observed,source=excluded.source,windows=excluded.windows '
                        'WHERE excluded.observed>=buckets.observed',
                        (provider, bucket, now, source, json.dumps(windows)))

    def unknown(self, provider, reason, source, now=None):
        self.put(provider, 'codex' if provider == 'sol' else 'subscription',
                 [dict(name='unreported', observation_reason=reason)], source,
                 time.time() if now is None else now)

    def codex(self, data, source='codex.read', now=None):
        now = time.time() if now is None else now
        if not isinstance(data, dict) or not any(k in data for k in ('rateLimits', 'rateLimitsByLimitId')):
            raise ValueError('Codex response has no rate limit snapshot')
        buckets = data.get('rateLimitsByLimitId')
        if not isinstance(buckets, dict) or not buckets:
            value = data.get('rateLimits', {})
            buckets = {value.get('limitId') or 'codex': value}
        for bucket, value in buckets.items():
            windows = []
            for name in ('primary', 'secondary'):
                raw = value.get(name)
                if isinstance(raw, dict):
                    windows.append(dict(name=name, used_percent=raw.get('usedPercent'),
                                        reset_at=raw.get('resetsAt'),
                                        window_seconds=raw['windowDurationMins'] * 60
                                        if number(raw.get('windowDurationMins')) else None))
            self.put('sol', bucket, windows, source, now)

    def status(self, now=None):
        now = time.time() if now is None else now
        result = {p: dict(state='unknown', reason='no official quota observation', windows=[])
                  for p in ('sol', 'sonnet')}
        for value in result.values():
            value['refresh'] = dict(last_attempt_at=None, last_success_at=None,
                                    duration_seconds=None, error=None, history=[])
            value['refresher'] = dict(process_running=_background is not None and _background.is_alive(),
                                      enabled=period() > 0, period_seconds=period(),
                                      startup_error=_background_error,
                                      seconds_since_attempt=None, scheduling_gap=None)
        if not self.path.exists():
            return result
        with contextlib.closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            rows = con.execute('SELECT provider,id,observed,source,windows FROM buckets ORDER BY provider,id').fetchall()
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='refresh_diagnostics'").fetchone():
                for provider, data in con.execute('SELECT provider,data FROM refresh_diagnostics'):
                    if provider in result:
                        result[provider]['refresh'] = json.loads(data)
        for value in result.values():
            attempt = value['refresh']['last_attempt_at']
            value['refresher']['seconds_since_attempt'] = None if attempt is None else max(0, now - attempt)
            value['refresher']['scheduling_gap'] = (None if attempt is None else
                now - attempt > period() + SONNET_PROBE_INTERVAL)
        for provider, bucket, observed, source, encoded in rows:
            ttl = SONNET_CLI_TTL if provider == 'sonnet' and source == 'claude.cli.usage' else TTL
            age = now - observed
            if provider == 'sonnet' and -SONNET_CLOCK_SKEW <= age < 0:
                age = 0
            for window in json.loads(encoded) or [dict(name='unreported')]:
                used, reset, duration = (window.get(k) for k in ('used_percent', 'reset_at', 'window_seconds'))
                reason = ('invalid observation time' if age < 0 else 'stale' if age > MAX_AGE else
                          'expired TTL' if age > ttl else 'missing or invalid fields'
                          if not (number(used) and 0 <= used <= 100 and number(reset) and
                                  number(duration) and duration > 0) else
                          'reset passed; refresh required' if reset <= now else None)
                if reason == 'missing or invalid fields':
                    reason = window.get('observation_reason') or reason
                result[provider]['windows'].append(dict(window, limit_id=bucket, observed_at=observed,
                    source=source, age_seconds=age, state='unknown' if reason else 'known', reason=reason,
                    used_percent=None if reason else used))
        for value in result.values():
            windows = value['windows']
            if windows:
                value['state'] = 'known' if all(w['state'] == 'known' for w in windows) else 'unknown'
                value['reason'] = None if value['state'] == 'known' else 'one or more windows unknown'
                value['unknown_reasons'] = sorted({w['reason'] for w in windows if w['reason']})
        return result

    def claude(self, data, sdk=False, now=None):
        now = time.time() if now is None else now
        durations = {'five_hour': 18000, 'seven_day': 604800, 'seven_day_sonnet': 604800}
        if sdk:
            info = data.get('rate_limit_info', {})
            name = info.get('rate_limit_type')
            if name not in durations:
                return  # opus/overage/API budgets do not describe the sonnet subscription
            used = info.get('utilization')
            windows = [dict(name=name, used_percent=used * 100 if number(used) else None,
                            reset_at=info.get('resets_at'), window_seconds=durations[name])]
            self.put('sonnet', name, windows, 'claude.sdk', now)
        else:
            limits = data.get('rate_limits') or {}
            # Replace both statusline windows together, including independently missing fields.
            windows = [dict(name=name, used_percent=(limits.get(name) or {}).get('used_percentage'),
                            reset_at=(limits.get(name) or {}).get('resets_at'), window_seconds=durations[name])
                       for name in ('five_hour', 'seven_day')]
            self.put('sonnet', 'subscription', windows, 'claude.statusline', now)


def refresh(cache, *, rpc=None, sessions=None, now=None, force=False, thread=None):
    """Refresh before observation TTL under a shared lock. Never launch a model.

    rpc must be an already initialized, authorized app-server connection. A
    bounded tail of the 20 most recently modified sessions is the fallback.
    """
    import fcntl
    now = time.time() if now is None else now
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache.path.with_suffix('.lock'), 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        lock.seek(0)
        previous = lock.read()
        if not force and previous and 0 <= now - float(previous) <= REFRESH_INTERVAL:
            return
        lock.seek(0)
        lock.truncate()
        lock.write(str(now))
        lock.flush()
        if rpc is not None:
            try:
                cache.codex(rpc.call('account/rateLimits/read', {}, timeout=3), now=now)
                return
            except Exception:
                pass  # Missing account access is unknown, never a worker launch failure.
        sessions = Path(sessions or os.environ.get('ORCHD_CODEX_SESSIONS', Path.home() / '.codex/sessions'))
        paths = heapq.nlargest(20, sessions.glob(f'**/rollout-*-{thread}.jsonl' if thread else '**/*.jsonl'),
                              key=lambda p: p.stat().st_mtime)
        latest = None
        for path in paths:
            try:
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 262144))
                    lines = stream.read().splitlines()
                for line in lines:
                    if b'"rate_limits"' not in line or b'"token_count"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get('payload', {})
                        if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                            continue
                        observed = datetime.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                        limits = payload.get('rate_limits') or {}
                        if isinstance(limits, dict) and observed <= now and (latest is None or observed > latest[0]):
                            latest = (observed, limits)
                    except (ValueError, KeyError, TypeError):
                        continue
            except OSError:
                continue
        if latest:
            observed, limits = latest
            value = {'limitId': limits.get('limit_id') or 'codex'}
            for name in ('primary', 'secondary'):
                raw = limits.get(name)
                if isinstance(raw, dict):
                    value[name] = dict(usedPercent=raw.get('used_percent'),
                                       windowDurationMins=raw.get('window_minutes'), resetsAt=raw.get('resets_at'))
            cache.codex({'rateLimits': value}, source='codex.session.token_count', now=observed)


def collect_codex_turn(cache, log):
    """Read only this exec worker's rollout, preserving server receipt time."""
    thread = None
    with Path(log).open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'thread.started' and event.get('thread_id'):
                thread = event['thread_id']  # Append-only log may include a previous retry's thread.
    if thread:
        refresh(cache, thread=thread, force=True)
    else:
        cache.unknown('sol', 'exec log has no thread.started receipt', 'codex.exec')


def claude_usage(cache, text, now=None):
    """Strict CLI /usage display adapter. Unsupported locales/formats stay unknown.

    The display omits year and seconds. Accept only a future reset within that
    window, and use the beginning of the displayed minute (expire early).
    """
    now = time.time() if now is None else now
    windows = []
    for label, name, duration in [('Current session', 'five_hour', 18000),
                                  ('Current week (all models)', 'seven_day', 604800)]:
        match = re.search(re.escape(label) + r': ([\d.]+)% used · resets '
                          r'([A-Z][a-z]{2}) (\d{1,2}) at (\d{1,2})(?::(\d{2}))?(am|pm) '
                          r'\(([^)]+)\)', text)
        if not match:
            raise ValueError('CLI /usage missing subscription windows or unsupported display format')
        month, day, hour, minute, period, zone = match.groups()[1:]
        tz = ZoneInfo(zone)
        local = datetime.datetime.fromtimestamp(now, tz)
        month_number = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].index(month) + 1
        hour = int(hour)
        if not 1 <= hour <= 12:
            raise ValueError('CLI /usage invalid reset hour')
        candidates = [datetime.datetime(year, month_number, int(day), hour % 12 + (12 if period == 'pm' else 0),
                                       int(minute or 0), tzinfo=tz).timestamp()
                      for year in (local.year, local.year + 1)]
        resets = [stamp for stamp in candidates if 0 < stamp - now <= duration]
        if len(resets) != 1:
            raise ValueError('CLI /usage reset is past or outside subscription window')
        windows.append(dict(name=name, used_percent=float(match[1]), reset_at=resets[0],
                            window_seconds=duration, reset_precision_seconds=60))
    cache.put('sonnet', 'subscription', windows, 'claude.cli.usage', now)


_stop = None  # threading.Event once a background refresher exists
_children = set()
_children_lock = threading.Lock()


def _stopping():
    return _stop is not None and _stop.is_set()


def _reap(process, grace=3):
    """Terminate a probe process group, escalating to kill; never raises."""
    if process.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(Exception):
                process.send_signal(sig)
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _run(cmd, timeout, **kwargs):
    """subprocess.run replacement whose child is registered so stop_background can reap it."""
    process = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    with _children_lock:
        _children.add(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        _reap(process)
        raise
    finally:
        with _children_lock:
            _children.discard(process)
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _reap_children():
    with _children_lock:
        children = list(_children)
    for process in children:
        _reap(process)


def sanitized_error(error):
    """Never persist arbitrary exception text, commands, paths or provider output."""
    message = str(error).lower()
    category = next((label for pattern, label in (
        ('timeout', 'timeout'), ('timed out', 'timeout'),
        ('auth', 'authentication failed'), ('login', 'authentication failed'),
        ('network', 'network failure'), ('connect', 'connection failure'),
        ('missing subscription', 'unsupported usage display'),
        ('zero turns required', 'local command required; zero turns required'),
        ('exited', 'CLI exited unsuccessfully')) if pattern in message), 'quota read failed')
    return dict(error_class=type(error).__name__, message=category)


def probe(cache, provider, binary=None):
    started, clock = time.time(), time.monotonic()
    error = None
    try:
        error = _probe(cache, provider, binary)
    except Exception as exc:
        error = exc
    if not _stopping():
        cache.record_attempt(provider, started, max(0, time.monotonic() - clock),
                             sanitized_error(error) if error else None)
    return error is None


def _probe(cache, provider, binary=None):
    """Explicit bounded CLI reads; no model turn, credentials or global edits."""
    if provider == 'sonnet':
        from .runtime import launch_env
        try:
            result = _run([binary or 'claude', '-p', '/usage', '--output-format', 'json',
                '--tools', '', '--no-session-persistence', '--setting-sources', '',
                '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}'], env=launch_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            if _stopping():
                return  # Shutting down: a killed probe is not an observation.
            if result.returncode:
                raise ValueError(f'CLI /usage exited {result.returncode}: {result.stderr}')
            data = json.loads(result.stdout)
            if (data.get('is_error') or data.get('num_turns') != 0 or
                    data.get('local_command') != 'usage' or data.get('duration_api_ms') != 0):
                raise ValueError('CLI /usage did not return a local slash-command result (zero turns required)')
            claude_usage(cache, data.get('result', ''))
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            # Preserve receipts (and their original age) when a read fails.
            if _stopping():
                return error
            if cache.status()['sonnet']['windows']:
                return error
            cache.unknown('sonnet', f'Claude /usage unavailable: {type(error).__name__}: {sanitized_error(error)["message"]}'
                          if isinstance(error, ValueError) else f'Claude /usage unavailable: {type(error).__name__}',
                          'claude.cli.usage')
            return error
        return
    from .app_transport import RPC
    with tempfile.TemporaryDirectory(prefix='orchd-quota-') as tmp:
        endpoint = str(Path(tmp) / 'rpc.sock')
        process = None
        rpc = None
        try:
            process = subprocess.Popen([binary or 'codex', 'app-server', '--listen', 'unix://' + endpoint],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            with _children_lock:
                _children.add(process)
            deadline = time.monotonic() + 10
            while not Path(endpoint).exists():
                if _stopping():
                    raise TimeoutError('stopped')
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise TimeoutError('Codex quota app-server did not publish socket')
                time.sleep(.05)
            rpc = RPC(endpoint)
            cache.codex(rpc.call('account/rateLimits/read', {}, timeout=3), source='codex.probe.read')
        except Exception as error:
            if not _stopping() and not cache.status()['sol']['windows']:
                cache.unknown('sol', f'Codex rateLimits/read unavailable: {type(error).__name__}', 'codex.probe.read')
            return error
        finally:
            if rpc is not None:
                rpc.close()
            if process is not None:
                _reap(process)
                with _children_lock:
                    _children.discard(process)


def period():
    try:
        return float(os.environ.get('ORCHD_QUOTA_REFRESH_SECONDS', BACKGROUND_PERIOD))
    except ValueError:
        return float(BACKGROUND_PERIOD)


def refresh_background(cache, *, now=None, binary=None, claude=None):
    """Gate successful reads per provider per period for the whole machine.

    The shared state file (`quota.periodic.lock`) holds per-provider success
    and attempt times, plus legacy aggregate timestamps. Whoever holds the exclusive
    lock runs the reads (so a background tick and a dispatch-triggered wake
    never overlap); everyone else, in any process, skips while the last
    success is younger than one period, and while a failed attempt is younger
    than SONNET_PROBE_INTERVAL. Sol prefers the Codex session tail and probes
    app-server only if that leaves it unknown; sonnet uses the bounded /usage
    probe. Failures are tolerated, never raised.
    """
    import fcntl
    now = time.time() if now is None else now
    try:
        cache.path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache.path.with_suffix('.periodic.lock'), 'a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            lock.seek(0)
            try:
                state = json.loads(lock.read() or '{}')
                success, attempt = float(state.get('success', -math.inf)), float(state.get('attempt', -math.inf))
            except (ValueError, TypeError, AttributeError):
                state = {}
                success = attempt = -math.inf
            providers = state.get('providers', {})
            if not providers and (0 <= now - success < period() or
                    0 <= now - attempt < min(SONNET_PROBE_INTERVAL, period())):
                return False
            attempted = False
            for provider in ('sol', 'sonnet'):
                if _stopping():
                    return False
                previous = providers.get(provider, {})
                last_success = previous.get('success', -math.inf)
                last_attempt = previous.get('attempt', -math.inf)
                if (0 <= now - last_success < period() or
                        0 <= now - last_attempt < min(SONNET_PROBE_INTERVAL, period())):
                    continue
                attempted = True
                started = time.monotonic()
                try:
                    if provider == 'sol':
                        refresh(cache, force=True, now=now)
                        ok = cache.status(now=now)['sol']['state'] == 'known'
                        if ok:
                            cache.record_attempt(provider, now, time.monotonic() - started, None)
                        else:
                            ok = probe(cache, provider, binary)
                    else:
                        ok = probe(cache, provider, claude or os.environ.get('ORCHD_CLAUDE'))
                    # Older collectors may not return an explicit outcome.
                    if ok is None:
                        ok = cache.status(now=now)[provider]['state'] == 'known'
                        cache.record_attempt(provider, now, time.monotonic() - started,
                            None if ok else dict(error_class='Unavailable', message='no known quota receipt'))
                except Exception as error:
                    ok = False
                    cache.record_attempt(provider, now, time.monotonic() - started, sanitized_error(error))
                providers[provider] = dict(attempt=now, **({'success': now} if ok else
                    {'success': last_success} if last_success > -math.inf else {}))
            if not attempted:
                return False
            snapshot = cache.status(now=now)
            if _stopping():
                return False
            ok = any(snapshot[p]['state'] == 'known' for p in ('sol', 'sonnet'))
            all_ok = all(providers.get(p, {}).get('success') == now for p in ('sol', 'sonnet'))
            state = dict(attempt=now, providers=providers,
                         success=now if all_ok else (success if success > -math.inf else None))
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps({k: v for k, v in state.items() if v is not None}))
            lock.flush()
            return ok
    except Exception as error:
        print(f'orchd: quota refresh unavailable: {sanitized_error(error)}', file=sys.stderr)
        return False


_background = None
_background_error = None
_wake = None


def start_background(cache=None, *, interval=None):
    """Start (once per process) a daemon thread running refresh_background periodically."""
    global _background, _stop, _wake, _background_error
    interval = period() if interval is None else interval
    if interval <= 0 or (_background is not None and _background.is_alive()):
        return _background
    _background_error = None
    stop, wake = _stop, _wake = threading.Event(), threading.Event()

    def loop():
        while not stop.is_set():
            try:
                refresh_background(cache or Cache())
            except Exception as error:
                print(f'orchd: quota refresh loop failed: {sanitized_error(error)}', file=sys.stderr)
            wake.wait(interval)  # a dispatch seeing unknown quota may wake us early
            wake.clear()
            # Waking never bypasses the gate: refresh_background still enforces one read per period.

    thread = threading.Thread(target=loop, name='orchd-quota-refresh', daemon=True)
    try:
        thread.start()
    except Exception as error:  # e.g. RuntimeError: cannot start new thread
        _background_error = sanitized_error(error)
        print(f'orchd: quota background refresh not started: {sanitized_error(error)}', file=sys.stderr)
        _background = _stop = _wake = None  # nothing runs; dispatch just sees unknown quota
        return None
    _background = thread
    return _background


def stop_background(timeout=5):
    """Stop the refresher thread and reap any probe subprocess; safe if never started."""
    global _background, _stop, _wake
    thread = _background
    if _stop is not None:
        _stop.set()
    if _wake is not None:
        _wake.set()
    _reap_children()
    if thread is not None and thread.ident is not None and thread is not threading.current_thread():
        thread.join(timeout)
        _reap_children()
    if thread is None or not thread.is_alive():
        _background = _stop = _wake = None  # nothing left running: a later start is fresh


def request_refresh():
    """Wake the background refresher (if this process runs one); never blocks."""
    if _wake is not None and _background is not None and _background.is_alive() and not _stopping():
        _wake.set()
        return True
    return False


# Remaining-percent scores this close (percentage points) count as a tie; ties prefer sol.
TIE_MARGIN = 5


def shadow(snapshot, *, backend='exec', task_type=None, author_model=None, now=None,
           applied=False, recent_limited=()):
    """One shared selection policy, optionally applied; never a quota reservation."""
    now = time.time() if now is None else now
    allowed = ['sol'] if backend == 'app-server' else ['sol', 'sonnet']
    review = task_type == 'review' or author_model is not None
    if review:
        if not author_model:
            return dict(selected_alias=None, reason='review author unknown; another provider required',
                        policy_version='quota-shadow-v1', applied=False, quota=snapshot)
        other = 'sonnet' if author_model.startswith('gpt-') else 'sol' if author_model.startswith('claude-') else None
        allowed = [p for p in allowed if p == other]
    usable = []
    for provider in allowed:
        value = snapshot[provider]
        windows = value['windows']
        if provider not in recent_limited and value['state'] == 'known' and windows and all(w['used_percent'] < 95 for w in windows):
            # Most constrained window's remaining percentage (below the 95% cap).
            usable.append((min(95 - w['used_percent'] for w in windows), provider == 'sol', provider))
    selected = None
    if usable:
        best = max(usable)[0]
        # Scores within TIE_MARGIN of the best are a tie: prefer sol, then the higher score.
        selected = max((u for u in usable if best - u[0] <= TIE_MARGIN), key=lambda u: (u[1], u[0]))[2]
    reason = ('review requires another provider; ' if review else '')
    sol_used = max((w['used_percent'] for w in snapshot['sol']['windows']
                    if w.get('state') == 'known' and number(w.get('used_percent'))), default=None)
    if (selected == 'sonnet' and 'sol' in allowed and 'sol' not in recent_limited and snapshot['sol']['state'] != 'known'
            and (sol_used is None or sol_used < 95)):
        selected = 'sol'
        reason += 'sol quota unknown (expired/stale); prefer sol, not limited or full; no capacity guarantee'
    elif selected:
        reason += 'known capacity with 5% margin; remaining-percent score (balances usage; provisional)'
    elif applied and allowed:
        # Apply the same policy's fallback rather than queueing on absent telemetry.
        used = {p: max((w['used_percent'] for w in snapshot[p]['windows']
                       if w.get('state') == 'known' and number(w.get('used_percent'))), default=None)
                for p in allowed}
        not_full = [p for p in allowed if p not in recent_limited and (used[p] is None or used[p] < 95)]
        candidates = not_full or [p for p in allowed if p not in recent_limited and used[p] is not None]
        selected = ('sol' if 'sol' in not_full else not_full[0]) if not_full else (
            min(candidates, key=lambda p: (used[p], p != 'sol')) if candidates else
            'sol' if 'sol' in allowed else allowed[0])
        reason += ('quota unknown/stale; fallback (prefer sol when not exhausted), no capacity guarantee' if not_full else
                   'allowed providers full; most remaining known percentage, tie/no evidence: sol (best-effort)')
    elif review:
        reason += 'wait: required provider unavailable, unknown, or backend incompatible'
    elif any(snapshot[p]['state'] == 'unknown' for p in allowed):
        selected = 'sol'
        reason += 'quota unknown; conservative sol preference, no capacity guarantee'
    else:
        reason += 'wait: known windows at or above 95% used'
    if applied and recent_limited:
        reason += '; recent rate limit within 10 minutes: ' + ','.join(sorted(recent_limited))
    if applied and selected == 'sonnet' and ('sol' in recent_limited or any(
            w.get('state') == 'known' and number(w.get('used_percent')) and w['used_percent'] >= 95
            for w in snapshot['sol']['windows'])):
        reason += '; sol at >=95% or recent rate limit; selected sonnet'
    return dict(selected_alias=selected, reason=reason, policy_version='quota-shadow-v1',
                applied=applied, quota=snapshot)


def dispatch_shadow(**kwargs):
    try:
        cache = Cache()
        if kwargs.get('applied') and kwargs.get('backend', 'exec') != 'app-server':
            # Dispatch only reads the cache. Empty/expired telemetry is unknown; wake the
            # background refresher for next time instead of probing while the caller waits.
            snapshot = cache.status(now=kwargs.get('now'))
            if any(snapshot[p]['state'] != 'known' for p in ('sol', 'sonnet')):
                request_refresh()
            return shadow(snapshot, **kwargs)
        return shadow(cache.status(now=kwargs.get('now')), **kwargs)
    except Exception:
        if kwargs.get('applied'):
            return shadow({p: dict(state='unknown', windows=[]) for p in ('sol', 'sonnet')}, **kwargs)
        return dict(selected_alias=None, reason='quota observation unavailable',
                    policy_version='quota-shadow-v1', applied=False)
