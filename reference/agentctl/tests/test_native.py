"""Native contracts: no tmux, no duplicate sends, persistent identity and safe stop."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_tmux_orch import ctl

native = ctl.native


class NativeContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="act-native-test-", dir="/tmp")
        self.home = self.temp.name
        self.paths = patch.multiple(ctl, HOME=self.home, DB_PATH=self.home + "/agentctl.db",
                                    MSG_DIR=self.home + "/msgs", EVENT_LOG=self.home + "/events.jsonl")
        self.paths.start()
        self.con = ctl.db()
        for wid, role, kind in (("orch", "orchestrator", "codex"), ("worker", "worker", "claude")):
            self.con.execute("INSERT INTO workers(id,kind,role,cwd,socket,target,state,created_at,backend,session_id) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)", (wid, kind, role, self.home, "unused", wid, "idle", ctl.now(), "native", "sid-" + wid))
        self.con.execute("INSERT INTO management VALUES(1,'native')")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.paths.stop()
        self.temp.cleanup()

    def message(self, mid="m1", state="queued", recipient="worker", kind="dispatch"):
        self.con.execute("INSERT INTO messages(id,sender,recipient,kind,summary,state,created_at) VALUES(?,?,?,?,?,?,?)",
                         (mid, "orchestrator", recipient, kind, "hello", state, ctl.now()))
        self.con.commit()
        return self.con.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def worker(self, wid="worker"):
        return ctl.get_worker(self.con, wid)

    def state(self, mid="m1"):
        return self.con.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()[0]

    def test_report_from_actual_orch_id_wakes_and_default_inbox_reads_it(self):
        self.message(state="acked")
        self.con.execute("UPDATE messages SET sender='orch' WHERE id='m1'")
        ctl.set_state(self.con, "orch", "busy")
        self.con.commit()
        with patch.object(native, "active", return_value=True), patch.object(native, "send") as send, \
                contextlib.redirect_stdout(io.StringIO()):
            ctl.cmd_report(SimpleNamespace(message="m1", status="done", summary="Hello inbox"))
            self.assertEqual(len(ctl.inbox_rows(self.con, "orchestrator")), 1)
            self.assertEqual(len(ctl.open_wakes(self.con, "orch")), 1)
            send.assert_not_called()
            ctl.set_state(self.con, "orch", "idle")
            ctl.pump_orchestrator(self.con)
            send.assert_called_once()
            self.con.commit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ctl.cmd_inbox(SimpleNamespace(recipient="orchestrator", mark_read=True))
        self.assertIn("Hello inbox", output.getvalue())
        self.assertEqual(ctl.inbox_rows(self.con, "orch"), [])
        self.assertEqual(ctl.open_wakes(self.con, "orch"), [])

    def test_orch_alias_reads_both_addresses_without_consuming_other_inboxes(self):
        self.message("legacy", recipient="orchestrator", kind="reply")
        self.message("actual", recipient="orch", kind="reply")
        self.message("human", recipient="nat", kind="reply")
        self.message("other", recipient="worker", kind="reply")
        self.message("wake", recipient="orch", kind="wake", state="sent")
        with contextlib.redirect_stdout(io.StringIO()):
            ctl.cmd_inbox(SimpleNamespace(recipient="orch", mark_read=True))
        self.assertEqual(self.state("legacy"), "done")
        self.assertEqual(self.state("actual"), "done")
        self.assertEqual(self.state("wake"), "done")
        self.assertEqual(self.state("human"), "queued")
        self.assertEqual(self.state("other"), "queued")
        self.assertEqual(ctl.inbox_rows(self.con, "orchestrator"), [])

    def test_busy_worker_holds_dispatch_then_idle_sends_without_touching_terminal(self):
        self.message()
        with patch.object(native, "active", return_value=True), patch.object(native, "send") as send, \
                patch.object(ctl, "tmux", side_effect=AssertionError("native called tmux")):
            ctl.set_state(self.con, "worker", "busy")
            self.assertEqual(ctl.pump_worker(self.con, self.worker()), "worker busy")
            send.assert_not_called()
            ctl.set_state(self.con, "worker", "idle")
            self.assertEqual(ctl.pump_worker(self.con, self.worker()), "sent m1")
            self.assertEqual(self.state(), "sent")
            self.assertIn("ack m1", send.call_args.args[-1])
            self.assertEqual(send.call_count, 1)

    def test_ack_arriving_before_sender_returns_is_not_downgraded(self):
        self.message()
        def receive(*args):
            # A separate connection can acquire a write lock during transport I/O.
            other = ctl.db()
            try:
                other.execute("BEGIN IMMEDIATE")
                ctl.ack_native(other, ctl.get_worker(other, "worker"), "[agentctl msg:m1]", "fixture receiver")
                other.commit()
            finally:
                other.close()
        with patch.object(native, "active", return_value=True), patch.object(native, "send", side_effect=receive):
            ctl.pump_worker(self.con, self.worker())
        self.assertEqual(self.state(), "acked")

    def test_uncertain_send_blocks_later_tasks_and_never_falls_back(self):
        self.message()
        self.message("m2")
        with patch.object(native, "active", return_value=True), \
                patch.object(native, "send", side_effect=TimeoutError("lost response")) as send, \
                patch.object(ctl, "send_line", side_effect=AssertionError("fallback")):
            self.assertEqual(ctl.pump_worker(self.con, self.worker()), "failed")
            self.assertEqual(self.state(), "unconfirmed")
            ctl.pump_worker(self.con, self.worker())
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.state("m2"), "queued")

    def test_no_ack_timeout_is_retained_and_blocks_next_dispatch(self):
        self.message(state="sent")
        self.con.execute("UPDATE messages SET sent_at='2000-01-01T00:00:00+00:00'")
        self.message("m2")
        with patch.object(native, "active", return_value=True), patch.object(native, "send") as send:
            ctl.pump_worker(self.con, self.worker())
            self.assertEqual(self.state(), "unconfirmed")
            send.assert_not_called()

    def test_report_and_idle_are_both_required_before_next_task(self):
        self.message(state="acked")
        self.message("m2")
        with patch.object(native, "active", return_value=True), patch.object(native, "send") as send:
            ctl.pump_worker(self.con, self.worker())
            send.assert_not_called()  # idle but no report
            self.con.execute("UPDATE messages SET state='done' WHERE id='m1'")
            ctl.set_state(self.con, "worker", "busy")
            ctl.pump_worker(self.con, self.worker())
            send.assert_not_called()  # reported but still executing
            ctl.set_state(self.con, "worker", "idle")
            ctl.pump_worker(self.con, self.worker())
            send.assert_called_once()

    def test_orchestrator_crash_pauses_new_work_without_stopping_workers(self):
        self.message()
        with patch.object(native, "active", side_effect=lambda c, db, w: w["id"] != "orch"), \
                patch.object(native, "stop") as stop, patch.object(native, "send") as send:
            self.assertIn("paused", ctl.pump_worker(self.con, self.worker()))
            self.assertEqual(self.state(), "queued")
            stop.assert_not_called()
            send.assert_not_called()

    def test_mode_switch_rejected_while_any_group_member_runs(self):
        with patch.object(ctl, "group_running", return_value=True):
            with self.assertRaises(SystemExit):
                ctl.select_backend(self.con, "tmux")
        self.con.rollback()
        self.assertEqual(self.con.execute("SELECT backend FROM management").fetchone()[0], "native")
        with patch.object(ctl, "group_running", return_value=False):
            ctl.select_backend(self.con, "tmux")
        self.assertEqual({r[0] for r in self.con.execute("SELECT backend FROM workers")}, {"tmux"})

    def test_native_hooks_from_viewing_clients_cannot_claim_or_stop_group(self):
        for event in ("SessionStart", "UserPromptSubmit", "SessionEnd"):
            with patch.dict(os.environ, AGENTCTL_WORKER="orch"), \
                    patch("sys.stdin", io.StringIO(json.dumps({"hook_event_name": event, "session_id": "foreign", "cwd": self.home}))), \
                    contextlib.redirect_stdout(io.StringIO()), patch.object(ctl, "spawn_watch") as watch:
                ctl.cmd_hook(SimpleNamespace(orchestrator=True, kind="codex"))
                watch.assert_not_called()
                self.assertEqual(self.worker("orch")["session_id"], "sid-orch")

    def test_stale_pid_identity_cannot_be_stopped(self):
        self.con.execute("INSERT INTO native_sessions(worker_id,generation,kind,endpoint,pid,process_start,log_path) "
                         "VALUES('worker','g','codex','/unused',123,'old','unused')")
        self.con.commit()
        with patch.object(ctl, "process_info", return_value={"pid": 123, "start": "new"}), patch.object(os, "kill") as kill:
            native.stop(ctl.CTX, self.con, self.worker())
            kill.assert_not_called()
        self.assertEqual(self.worker()["state"], "stopped")

    def test_busy_orchestrator_coalesces_reports_and_failed_wake_is_not_retried(self):
        self.message("reply1", recipient="orchestrator", kind="reply")
        ctl.set_state(self.con, "orch", "busy")
        self.con.commit()
        with patch.object(native, "active", return_value=True), patch.object(native, "send") as send:
            ctl.enqueue_wake(self.con, "first")
            ctl.enqueue_wake(self.con, "second")
            self.assertEqual(len(ctl.open_wakes(self.con, "orch")), 1)
            send.assert_not_called()
            ctl.set_state(self.con, "orch", "idle")
            send.side_effect = TimeoutError("uncertain")
            ctl.pump_orchestrator(self.con)
            ctl.enqueue_wake(self.con, "third")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(len(ctl.inbox_rows(self.con, "orchestrator")), 1)

    def test_native_default_and_explicit_tmux_selection(self):
        with patch.object(ctl, "group_running", return_value=False), patch.object(ctl, "cmd_native_orch") as launch, \
                patch.dict(os.environ, {}, clear=True), patch.object(os, "getcwd", return_value=ctl.REPO):
            ctl.cmd_orch(SimpleNamespace(kind="codex", mode=None, args=[], no_attach=True, takeover=False, resume=False))
            launch.assert_called_once()

    def test_duplicate_send_reservation_cannot_deliver_twice(self):
        m = self.message()
        with patch.object(native, "send") as send:
            self.assertTrue(ctl.native_delivery(self.con, self.worker(), m, "first"))
            self.assertFalse(ctl.native_delivery(self.con, self.worker(), m, "duplicate"))
            send.assert_called_once()

    def test_native_stop_keeps_conversation_and_interrupts_pending_work(self):
        self.message(state="acked")
        self.message("m2")
        self.con.execute("INSERT INTO native_sessions(worker_id,generation,kind,session_id,endpoint,log_path) "
                         "VALUES('worker','g','claude','saved-conversation','/unused','unused')")
        self.con.commit()
        native.stop(ctl.CTX, self.con, self.worker())
        self.assertEqual(native.row(self.con, "worker")["session_id"], "saved-conversation")
        self.assertEqual(self.state(), "interrupted")
        self.assertEqual(self.state("m2"), "interrupted")
        self.assertEqual(self.worker()["state"], "stopped")

    def test_resume_requires_matching_saved_provider(self):
        self.con.execute("INSERT INTO native_sessions(worker_id,generation,kind,session_id,endpoint,log_path) "
                         "VALUES('worker','g','codex','saved-conversation','/unused','unused')")
        self.con.commit()
        with self.assertRaisesRegex(RuntimeError, "no saved claude"):
            native.start(ctl.CTX, self.con, self.worker(), resume=True)
        self.con.rollback()

    def test_terminal_disconnect_does_not_change_native_identity(self):
        self.con.execute("INSERT INTO native_sessions(worker_id,generation,kind,session_id,job_id,endpoint,log_path) "
                         "VALUES('worker','g','claude','sid','job','/unused','unused')")
        self.con.commit()
        with patch.object(native, "active", return_value=True), patch.object(ctl, "live_owner", return_value=None):
            self.assertEqual(native.attach_argv(ctl.CTX, self.con, self.worker()), ["claude", "attach", "job"])
            # No TUI owner is required: the next attach targets the exact same job.
            self.assertEqual(native.attach_argv(ctl.CTX, self.con, self.worker()), ["claude", "attach", "job"])

    def test_codex_attach_uses_worker_directory_from_another_cwd(self):
        self.con.execute("INSERT INTO native_sessions(worker_id,generation,kind,session_id,endpoint,log_path) "
                         "VALUES('orch','g','codex','sid','/private/socket','unused')")
        self.con.commit()
        with patch.object(native, "active", return_value=True):
            self.assertEqual(native.attach_argv(ctl.CTX, self.con, self.worker("orch")),
                             ["codex", "resume", "--remote", "unix:///private/socket",
                              "--cd", self.home, "sid"])

    def test_native_read_inbox_closes_wake_without_reporting_it(self):
        self.message("reply", recipient="orchestrator", kind="reply")
        self.message("wake", state="sent", recipient="orch", kind="wake")
        with contextlib.redirect_stdout(io.StringIO()):
            ctl.cmd_inbox(SimpleNamespace(recipient="orchestrator", mark_read=True))
        self.assertEqual(self.state("wake"), "done")
        self.assertEqual(ctl.inbox_rows(self.con, "orchestrator"), [])

    def test_explicit_group_stop_stops_every_member_without_tmux(self):
        with patch.object(native, "stop") as stop, patch.object(ctl, "tmux", side_effect=AssertionError("native used tmux")), \
                contextlib.redirect_stdout(io.StringIO()):
            ctl.cmd_stop(SimpleNamespace())
        self.assertEqual({call.args[2]["id"] for call in stop.call_args_list}, {"orch", "worker"})

    def test_management_menu_defaults_native_and_can_select_tmux(self):
        self.assertEqual(ctl.choose_backend(io.StringIO("\n"), io.StringIO()), "native")
        self.assertEqual(ctl.choose_backend(io.StringIO("2\n"), io.StringIO()), "tmux")
        self.assertIsNone(ctl.choose_backend(io.StringIO("q\n"), io.StringIO()))

    def test_blocked_report_is_idempotent(self):
        self.message(state="acked")
        with patch.object(ctl, "notify_inbox", return_value="fixture"), contextlib.redirect_stdout(io.StringIO()):
            args = SimpleNamespace(message="m1", status="blocked", summary="need input")
            ctl.cmd_report(args)
            ctl.cmd_report(args)
        self.assertEqual(self.state(), "closed-blocked")
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE reply_to='m1'").fetchone()[0], 1)


class WebSocketProtocol(unittest.TestCase):
    def test_upgrade_masking_fragmentation_ping_and_rpc_notifications(self):
        with tempfile.TemporaryDirectory(prefix="act-ws-", dir="/tmp") as tmp:
            path = tmp + "/s"
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(path)
            listener.listen()
            errors = []
            def server():
                try:
                    s, _ = listener.accept()
                    with s:
                        data = b""
                        while b"\r\n\r\n" not in data:
                            data += s.recv(4096)
                        key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
                        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                        s.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                        f = s.makefile("rb")
                        try:
                            head = f.read(2)
                            self.assertTrue(head[1] & 128)
                            length = head[1] & 127
                            if length == 126:
                                length = struct.unpack("!H", f.read(2))[0]
                            mask, body = f.read(4), f.read(length)
                            request = json.loads(bytes(c ^ mask[i % 4] for i, c in enumerate(body)))
                            note = json.dumps({"method": "turn/started"}).encode()
                            reply = json.dumps({"id": request["id"], "result": {"ok": True}}).encode()
                            s.sendall(bytes([0x81, len(note)]) + note)
                            s.sendall(b"\x89\x01x")
                            half = len(reply) // 2
                            s.sendall(bytes([0x01, half]) + reply[:half] + bytes([0x80, len(reply) - half]) + reply[half:])
                            f.read(7)  # masked pong
                        finally:
                            f.close()
                except BaseException as e:
                    errors.append(e)
            thread = threading.Thread(target=server)
            thread.start()
            ws = native.UnixWebSocket(path)
            try:
                notes = []
                self.assertEqual(ws.request("probe", {}, notes.append), {"ok": True})
                self.assertEqual(notes, [{"method": "turn/started"}])
            finally:
                ws.close()
                listener.close()
                thread.join(timeout=5)
            if errors:
                raise errors[0]


if __name__ == "__main__":
    unittest.main()
