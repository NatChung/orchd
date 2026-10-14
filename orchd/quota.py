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
import subprocess
import tempfile
from zoneinfo import ZoneInfo
from pathlib import Path

from . import store

TTL = 60
MAX_AGE = 300
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
        return con

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
        if not self.path.exists():
            return result
        with contextlib.closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            rows = con.execute('SELECT provider,id,observed,source,windows FROM buckets ORDER BY provider,id').fetchall()
        for provider, bucket, observed, source, encoded in rows:
            age = now - observed
            for window in json.loads(encoded) or [dict(name='unreported')]:
                used, reset, duration = (window.get(k) for k in ('used_percent', 'reset_at', 'window_seconds'))
                reason = ('invalid observation time' if age < 0 else 'stale' if age > MAX_AGE else
                          'expired TTL' if age > TTL else 'missing or invalid fields'
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


def probe(cache, provider, binary=None):
    """Explicit bounded CLI reads; no model turn, credentials or global edits."""
    if provider == 'sonnet':
        from .runtime import launch_env
        try:
            result = subprocess.run([binary or 'claude', '-p', '/usage', '--output-format', 'json',
                '--tools', '', '--no-session-persistence', '--setting-sources', '',
                '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}'], env=launch_env(),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise ValueError(f'CLI /usage exited {result.returncode}')
            data = json.loads(result.stdout)
            if (data.get('is_error') or data.get('num_turns') != 0 or
                    data.get('local_command') != 'usage' or data.get('duration_api_ms') != 0):
                raise ValueError('CLI /usage did not return a local slash-command result (zero turns required)')
            claude_usage(cache, data.get('result', ''))
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            cache.unknown('sonnet', f'Claude /usage unavailable: {type(error).__name__}: {error}'
                          if isinstance(error, ValueError) else f'Claude /usage unavailable: {type(error).__name__}',
                          'claude.cli.usage')
        return
    from .app_transport import RPC
    with tempfile.TemporaryDirectory(prefix='orchd-quota-') as tmp:
        endpoint = str(Path(tmp) / 'rpc.sock')
        process = None
        rpc = None
        try:
            process = subprocess.Popen([binary or 'codex', 'app-server', '--listen', 'unix://' + endpoint],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 10
            while not Path(endpoint).exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise TimeoutError('Codex quota app-server did not publish socket')
                time.sleep(.05)
            rpc = RPC(endpoint)
            cache.codex(rpc.call('account/rateLimits/read', {}, timeout=3), source='codex.probe.read')
        except Exception as error:
            cache.unknown('sol', f'Codex rateLimits/read unavailable: {type(error).__name__}', 'codex.probe.read')
        finally:
            if rpc is not None:
                rpc.close()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def shadow(snapshot, *, backend='exec', task_type=None, author_model=None, now=None):
    """A provisional observation, not a reservation or permission to launch."""
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
        if value['state'] == 'known' and windows and all(w['used_percent'] < 95 for w in windows):
            # Most constrained window's remaining percentage per second until reset.
            score = min((95 - w['used_percent']) / max(1, w['reset_at'] - now) for w in windows)
            usable.append((score, provider == 'sol', provider))
    selected = max(usable)[2] if usable else None
    reason = ('review requires another provider; ' if review else '')
    if selected:
        reason += 'known capacity with 5% margin; remaining capacity/reset-time score (provisional)'
    elif review:
        reason += 'wait: required provider unavailable, unknown, or backend incompatible'
    elif any(snapshot[p]['state'] == 'unknown' for p in allowed):
        selected = 'sol'
        reason += 'quota unknown; conservative sol preference, no capacity guarantee'
    else:
        reason += 'wait: known windows at or above 95% used'
    return dict(selected_alias=selected, reason=reason, policy_version='quota-shadow-v1',
                applied=False, quota=snapshot)


def dispatch_shadow(**kwargs):
    try:
        return shadow(Cache().status(), **kwargs)
    except Exception:
        return dict(selected_alias=None, reason='quota observation unavailable',
                    policy_version='quota-shadow-v1', applied=False)
