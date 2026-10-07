"""Issue #72: an Orch Claude's daemon retired for idling is resumed in place; a stopped one never is."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import core, orch_revive, store
from orchd.runtime import Runtime
from tests.test_orchd import FakeRuntime


class GoneOnce(FakeRuntime):
    """After `gone` is set, the next send hits a retired Orch's missing socket."""

    def __init__(self):
        super().__init__()
        self.gone = 0

    def send_uds(self, path, session_id, text):
        if self.gone:
            self.gone -= 1
            raise FileNotFoundError(path)
        super().send_uds(path, session_id, text)


class ReviveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = GoneOnce()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def task(self):
        return core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="demo", title="T", instructions="i",
                             done_when="d", model="sonnet", model_reason="r", task_type="outward")["id"]

    def test_worker_report_resumes_a_retired_orch_and_wakes_it(self):
        task = self.task()
        self.rt.sent.clear()
        self.rt.gone = 1
        self.assertTrue(core.report(self.con, self.rt, task, "done", "shipped", None))
        self.assertEqual(self.rt.resumed_orchs, [(self.orch, "claude-opus-5-5", "orchsession")])
        (path, session, text), = self.rt.sent
        self.assertEqual((path, session), (f"/tmp/orchd-o-{self.orch}/o.sock", "resumed1-session"))
        self.assertIn("done: shipped", text)
        self.assertEqual(store.get_orch(self.con, self.orch)["job_id"], "resumed1")

    def test_stopped_orch_is_never_resumed(self):
        store.stop_orch(self.con, self.orch)
        self.rt.gone = 1
        with self.assertRaises(FileNotFoundError):
            orch_revive.send(self.con, self.rt, self.orch, "hi")
        with self.assertRaisesRegex(ValueError, "orch-stop"):
            orch_revive.revive(self.con, self.rt, self.orch, seen_job="orchjob")
        self.assertFalse(getattr(self.rt, "resumed_orchs", []))

    def test_second_reviver_finds_the_new_job_and_spawns_nothing(self):
        first = orch_revive.revive(self.con, self.rt, self.orch, seen_job="orchjob")
        again = orch_revive.revive(self.con, self.rt, self.orch, seen_job="orchjob")
        self.assertEqual((first["job_id"], again["job_id"]), ("resumed1", "resumed1"))
        self.assertEqual(len(self.rt.resumed_orchs), 1)

    def test_alive_orch_is_left_alone_without_a_failed_send(self):
        self.rt.jobs["orchjob"] = {"pid": 4242, "status": "idle"}
        self.assertEqual(orch_revive.revive(self.con, self.rt, self.orch)["job_id"], "orchjob")
        self.assertFalse(getattr(self.rt, "resumed_orchs", []))

    def test_orch_changed_during_resume_stops_the_new_job(self):
        start = self.rt.start_orch

        def stopped_meanwhile(*args, **kw):
            store.stop_orch(self.con, self.orch)
            return start(*args, **kw)

        with mock.patch.object(self.rt, "start_orch", side_effect=stopped_meanwhile):
            with self.assertRaisesRegex(RuntimeError, "changed while reviving"):
                orch_revive.revive(self.con, self.rt, self.orch, seen_job="orchjob")
        self.assertEqual(self.rt.stopped, ["resumed1"])
        self.assertEqual(store.get_orch(self.con, self.orch)["job_id"], "orchjob")


class RuntimeResumeTest(unittest.TestCase):
    def start(self, rt, sock, home, **kw):
        with mock.patch.object(rt, "orch_socket_path", return_value=sock), \
                mock.patch.object(rt, "start_claude", return_value=("job", "session")) as start:
            rt.start_orch("o38", "opus", home, **kw)
        return start.call_args.args[3]

    def test_resume_passes_the_session_and_clears_a_dead_socket(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:  # AF_UNIX paths must stay short
            home = Path(tmp).resolve()
            sock = str(home / "o.sock")
            with socket.socket(socket.AF_UNIX) as old:
                old.bind(sock)  # left behind by the retired Orch: the file stays, nobody listens
            args = self.start(Runtime(), sock, home, resume="old-session")
            self.assertEqual(args[:2], ["--resume", "old-session"])
            self.assertFalse(Path(sock).exists())
            self.assertNotIn("--resume", self.start(Runtime(), sock, home))

    def test_resume_refuses_a_socket_someone_still_listens_on(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            sock = str(Path(tmp) / "o.sock")
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(sock)
                server.listen()
                with self.assertRaisesRegex(RuntimeError, "still accepts"):
                    self.start(Runtime(), sock, Path(tmp).resolve(), resume="old-session")
            self.assertTrue(Path(sock).exists())


if __name__ == "__main__":
    unittest.main()
