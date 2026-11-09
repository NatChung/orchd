"""Receive-only channel process with a single-threaded LINE HTTP loop."""
import signal
import threading
import tomllib
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from . import management, policy
from .adapters.line import LineReceiver, MAX_WEBHOOK_BYTES
from .adapters.runner import AdapterRunner
from .adapters.slack import SlackAdapter
from .secret_store import WindowsSecretStore
from .spool import Spool


def run_loop(http, receivers, stop):
    try:
        while not stop.is_set():
            http.handle_request()
            for receiver in receivers:
                receiver.drain()
    finally:
        http.server_close()


def slack_options(layout, source_id, routes):
    from decimal import Decimal, InvalidOperation
    config = layout.get('slack', {}).get(source_id, {})
    activation = config.get('activation')
    identities = config.get('self_identities', [])
    try:
        value = Decimal(activation) if isinstance(activation, str) else Decimal(0)
        if not value.is_finite() or value <= 0: raise ValueError()
    except (InvalidOperation, ValueError):
        raise ValueError('Slack source requires a positive activation timestamp') from None
    if not isinstance(identities, list) or any(not isinstance(x, str) or not x for x in identities):
        raise ValueError('invalid Slack self identities')
    conversations = sorted({r['conversation_ref'] for r in routes if r['source_id'] == source_id})
    if '*' in conversations: raise ValueError('Slack history requires explicit route conversations')
    return dict(conversations=tuple(conversations), self_identities=tuple(identities), activation=activation)


def run(svc, *, host='127.0.0.1', port=8080, stop=None, provider=None):
    home = Path(svc.channel_home)
    with (home / 'channels.toml').open('rb') as stream:
        layout = tomllib.load(stream)['channels']
    if layout.get('schema_version') != 1: raise ValueError('unsupported channel layout')
    provider = provider or WindowsSecretStore()
    stop = stop or threading.Event()
    op = policy.Caller(role='operator')
    source_rows = [row for row in management.sources(svc, op) if row['enabled'] and row['provider'] in ('slack', 'line')]
    routes = svc.routes(caller=op)
    options = {row['id']: slack_options(layout, row['id'], routes) for row in source_rows if row['provider'] == 'slack'}
    receivers, spools, threads = {}, [], []
    old_handlers = {}
    failures = []
    try:
        for row in source_rows:
            sid = row['id']
            caller = policy.Caller(role='adapter', sources=frozenset({sid}), identity=row['adapter_id'])
            if row['provider'] == 'line':
                spool = Spool(home / 'line' / 'state' / (sid + '.sqlite3'), sid)
                spools.append(spool)
                receivers[sid] = LineReceiver(svc, sid, caller, spool, provider)
            else:
                factory = svc.session_factory()
                def worker(row=row, caller=caller, factory=factory):
                    session = adapter = None
                    try:
                        session = factory()
                        adapter = SlackAdapter(session, row['id'], caller, None, provider, **options[row['id']])
                        result = adapter.connect()
                        if result and result.get('status') == 'busy':
                            raise RuntimeError('Slack source lease unavailable')
                        AdapterRunner(adapter).run(stop)
                    except Exception:
                        failures.append(row['id'])
                        stop.set()
                    finally:
                        try:
                            if adapter is not None: adapter.close()
                        except Exception:
                            failures.append(row['id'])
                            stop.set()
                        finally:
                            if session is not None: session.close()
                thread = threading.Thread(target=worker, name='slack-'+sid)
                thread.start(); threads.append(thread)
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                sid = self.path.removeprefix('/webhook/') if self.path.startswith('/webhook/') else None
                if self.path == '/webhook' and len(receivers) == 1: sid = next(iter(receivers))
                if sid not in receivers: self.send_error(404); return
                try: size = int(self.headers.get('Content-Length', '-1'))
                except ValueError: size = -1
                if not 0 <= size <= MAX_WEBHOOK_BYTES: self.send_error(413); return
                self.connection.settimeout(10)
                raw = self.rfile.read(size)
                status = receivers[sid].receive(raw, self.headers.get('X-Line-Signature')) if len(raw) == size else 400
                self.send_response(status); self.end_headers()
            def log_message(self, *args): pass
        http = HTTPServer((host, port), Handler)
        http.timeout = 1
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        run_loop(http, list(receivers.values()), stop)
        if failures:
            raise RuntimeError('Slack adapter failed: ' + ', '.join(failures))
    finally:
        stop.set()
        for thread in threads: thread.join()
        for spool in spools: spool.close()
        for sig, handler in old_handlers.items(): signal.signal(sig, handler)
