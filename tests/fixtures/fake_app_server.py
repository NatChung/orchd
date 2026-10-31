"""Stand-in for `codex app-server --listen unix://PATH`: a minimal WebSocket JSON-RPC server.

Records its working directory and every turn/start to $FAKE_APP_SERVER_LOG (one JSON per line).
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def log(**fields):
    with open(os.environ['FAKE_APP_SERVER_LOG'], 'a') as out:
        out.write(json.dumps(fields) + '\n')


def read_exact(conn, count):
    data = b''
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def read_frame(conn):
    head = read_exact(conn, 2)
    opcode, length = head[0] & 0xF, head[1] & 0x7F
    if length == 126:
        length = struct.unpack('>H', read_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack('>Q', read_exact(conn, 8))[0]
    mask = read_exact(conn, 4) if head[1] & 0x80 else b'\0\0\0\0'
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(conn, length)))
    return opcode, payload


def send(conn, message):
    body = json.dumps(message).encode()
    head = b'\x81'
    if len(body) < 126:
        head += bytes([len(body)])
    elif len(body) < 65536:
        head += b'\x7e' + struct.pack('>H', len(body))
    else:
        head += b'\x7f' + struct.pack('>Q', len(body))
    conn.sendall(head + body)


def handshake(conn):
    request = b''
    while b'\r\n\r\n' not in request:
        request += conn.recv(4096)
    key = next(line.split(b':', 1)[1].strip() for line in request.split(b'\r\n')
               if line.lower().startswith(b'sec-websocket-key'))
    accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
    conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                 b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')


def main():
    path = sys.argv[sys.argv.index('--listen') + 1].removeprefix('unix://')
    log(event='start', cwd=os.path.realpath(os.getcwd()))
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(4)
    turns = 0
    while True:
        conn, _ = server.accept()
        try:
            handshake(conn)
            while True:
                opcode, payload = read_frame(conn)
                if opcode == 8:
                    break
                if opcode != 1:
                    continue
                msg = json.loads(payload)
                if 'id' not in msg:
                    continue
                method, params, result = msg['method'], msg.get('params', {}), {}
                if method == 'thread/start':
                    result = {'thread': {'id': 'thr'}}
                elif method == 'thread/read':
                    result = {'thread': {'id': 'thr', 'status': {'type': 'idle'}, 'turns': []}}
                elif method == 'turn/start':
                    turns += 1
                    log(event='turn/start', text=params['input'][0]['text'])
                    result = {'turn': {'id': f't{turns}', 'status': 'inProgress'}}
                send(conn, {'id': msg['id'], 'result': result})
                if method == 'turn/start':
                    time.sleep(.2)
                    send(conn, {'method': 'turn/completed', 'params': {
                        'threadId': 'thr', 'turn': {'id': f't{turns}', 'status': 'completed', 'error': None}}})
        except EOFError:
            pass
        finally:
            conn.close()


main()
