import json
import queue
import threading
import unittest
from unittest.mock import patch

from orchd.app_transport import RPC, RPCError


class Wire:
    def __init__(self):self.received=queue.Queue();self.sent=[]
    def send(self,raw):
        msg=json.loads(raw);self.sent.append(msg)
        if 'id' in msg and 'method' in msg:
            self.received.put(json.dumps({'method':'turn/completed','params':{'turn':{'id':'old'}}}))
            self.received.put(json.dumps({'id':msg['id'],'result':{}}))
    def recv(self):return self.received.get()
    def close(self):self.received.put('')


class TransportTest(unittest.TestCase):
    def test_unsolicited_events_do_not_consume_rpc_response(self):
        wire=Wire()
        with patch('orchd.app_transport.socket.socket') as sock, \
             patch('websocket.create_connection',return_value=wire):
            rpc=RPC('/private/app.sock')
            self.assertEqual(rpc.call('turn/interrupt',{'threadId':'thread','turnId':'turn'}),{})
            self.assertEqual(rpc.events.get()['method'],'turn/completed')
            sock.return_value.connect.assert_called_once_with('/private/app.sock')
            self.assertEqual(wire.sent[1]['method'],'initialized')
            rpc.close()

    def test_disconnect_wakes_waiter_with_uncertainty(self):
        wire=Wire()
        with patch('orchd.app_transport.socket.socket'),patch(
                'websocket.create_connection',return_value=wire):
            rpc=RPC('/private/app.sock');wire.received.put('');rpc.reader.join(1)
            with self.assertRaises(ConnectionError):rpc.call('thread/read',{})
            rpc.close()
