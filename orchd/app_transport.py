"""Codex JSON-RPC over WebSocket on a private Unix socket (0.160.1).

The reader never writes SQLite or takes delivery locks. Responses and events remain
separate, so interrupt can wait for completion without a callback lock inversion.
"""
import json
import queue
import socket
import threading
import time


class RPCError(RuntimeError):
    pass


class RPC:
    def __init__(self, endpoint):
        import websocket
        self.websocket = websocket
        peer = socket.socket(socket.AF_UNIX)
        peer.settimeout(10)
        try:
            peer.connect(endpoint)
            self.ws = websocket.create_connection('ws://localhost/', socket=peer, timeout=1,
                                                   enable_multithread=True)
        except Exception:
            peer.close()
            raise
        self.events = queue.Queue()
        self.responses = {}
        self.condition = threading.Condition()
        self.sequence = 0
        self.error = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.call('initialize', {'clientInfo': {'name': 'orchd', 'version': '0.1.0'},
                                 'capabilities': {'experimentalApi': True}})
        self.notify('initialized', {})

    def _read(self):
        try:
            while True:
                try:
                    raw = self.ws.recv()
                except self.websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise EOFError('app-server disconnected')
                msg = json.loads(raw)
                if 'id' in msg and 'method' not in msg:
                    with self.condition:
                        self.responses[msg['id']] = msg
                        self.condition.notify_all()
                elif 'id' in msg:
                    # Workers use approval never. Fail closed if a plugin/server still asks.
                    self.ws.send(json.dumps({'id': msg['id'], 'error': {
                        'code': -32601, 'message': 'orchd does not grant interactive RPC approvals'}}))
                    self.events.put(msg)
                else:
                    self.events.put(msg)
        except Exception as exc:
            with self.condition:
                self.error = exc
                self.condition.notify_all()

    def notify(self, method, params):
        self.ws.send(json.dumps({'method': method, 'params': params}))

    def call(self, method, params, timeout=20):
        with self.condition:
            self.sequence += 1
            seq = self.sequence
            self.ws.send(json.dumps({'id': seq, 'method': method, 'params': params}))
            deadline = time.monotonic() + timeout
            while seq not in self.responses:
                if self.error:
                    raise ConnectionError(str(self.error))
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f'{method}: response uncertain')
                self.condition.wait(left)
            response = self.responses.pop(seq)
        if 'error' in response:
            raise RPCError(str(response['error']))
        return response['result']

    def close(self):
        self.ws.close()
        self.reader.join(2)
