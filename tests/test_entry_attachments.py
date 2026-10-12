import concurrent.futures
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import entry, entry_attachments as attachments, paths, store
from orchd.runtime import Runtime
from tests import test_entry as entry_tests

BODY = entry_tests.BODY


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.image_root = self.base / "claude-test" / "synthetic-home"
        self.path_patch = patch.object(paths, "orch_images", return_value=self.image_root)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.source = self.base / "截圖 with spaces.png"
        self.source.write_bytes(b"synthetic-image\x00\xff")

    def capture(self, sources=None, message=1, entry_id="desktop"):
        return attachments.capture(entry_id, message, sources or [str(self.source)])

    def described(self, sources=None, message=1, entry_id="desktop"):
        return attachments.describe(entry_id, message, sources or [str(self.source)])

    def directory(self, message=1, entry_id="desktop"):
        return self.image_root / "attachments" / attachments._entry_name(entry_id) / str(message)

    def test_private_exact_copy_survives_source_removal_and_repeated_capture(self):
        expected = self.source.read_bytes()
        self.assertIsNone(self.capture())
        self.source.unlink()
        self.assertIsNone(self.capture())
        items, omitted = self.described()
        copy = Path(items[0]["cached_path"])
        self.assertEqual(copy.read_bytes(), expected)
        self.assertEqual(items[0]["source_path"], str(self.source))
        self.assertEqual(omitted, 0)
        for p in [self.directory(), self.directory().parent, self.directory().parent.parent]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o700)
        for p in [copy, self.directory() / "manifest.json"]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(self.directory().iterdir())), 2)

    def test_multiple_images_and_count_limit(self):
        self.capture([str(self.source)] * 12)
        items, omitted = self.described([str(self.source)] * 12)
        self.assertEqual(len(items), 10)
        self.assertEqual(len({i["cached_path"] for i in items}), 10)
        self.assertEqual(omitted, 2)

    def test_missing_and_oversize_sources_are_recorded_without_partial_files(self):
        missing = self.base / "missing.png"
        with patch.object(attachments, "MAX_BYTES", 4):
            self.capture([str(missing), str(self.source)])
        items, _ = self.described()
        self.assertEqual([i["error"] for i in items], ["source_missing", "too_large"])
        self.assertEqual([p.name for p in self.directory().iterdir()], ["manifest.json"])

    def test_source_growth_is_bounded_even_when_initial_stat_is_small(self):
        self.source.write_bytes(b"12345")
        real_stat = os.fstat
        def smaller(fd):
            s = real_stat(fd)
            if s.st_ino == self.source.stat().st_ino:
                fields = list(s)
                fields[6] = 1
                return os.stat_result(fields)
            return s
        with patch.object(attachments, "MAX_BYTES", 4), patch.object(attachments.os, "fstat", side_effect=smaller):
            self.capture()
        self.assertEqual(self.described()[0][0]["error"], "too_large")

    def test_rejects_source_symlinks_including_parent_and_traversal(self):
        link = self.base / "linked.png"
        link.symlink_to(self.source)
        folder = self.base / "linked-dir"
        folder.symlink_to(self.base, target_is_directory=True)
        sources = [str(link), str(folder / self.source.name), str(self.base / "unused" / ".." / self.source.name),
                   self.source.name, str(self.base)]
        self.capture(sources)
        self.assertTrue(all(i["status"] == "failed" for i in self.described(sources)[0]))
        self.assertEqual(self.source.read_bytes(), b"synthetic-image\x00\xff")

    def test_rejects_fifo_without_blocking(self):
        fifo = self.base / "input.png"
        os.mkfifo(fifo)
        self.capture([str(fifo)])
        self.assertEqual(self.described([str(fifo)])[0][0]["error"], "unsafe_source")

    def test_rejects_another_owners_source(self):
        real_stat = os.fstat
        def other_owner(fd):
            s = real_stat(fd)
            if s.st_ino == self.source.stat().st_ino:
                fields = list(s)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return s
        with patch.object(attachments.os, "fstat", side_effect=other_owner):
            self.capture()
        self.assertEqual(self.described()[0][0]["error"], "unsafe_source")

    def test_destination_symlink_is_never_followed(self):
        self.image_root.mkdir(parents=True)
        outside = self.base / "outside"
        outside.mkdir()
        (self.image_root / "attachments").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.capture(), "cache_unavailable")
        self.assertEqual(list(outside.iterdir()), [])

    def test_entry_and_message_symlinks_are_rejected(self):
        self.capture()
        outside = self.base / "outside"
        outside.mkdir()
        (self.directory().parent / "2").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.capture(message=2), "cache_unavailable")
        (self.image_root / "attachments" / "other").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.capture(entry_id="other"), "cache_unavailable")
        self.assertEqual(list(outside.iterdir()), [])

    def test_unsafe_entry_id_is_encoded_and_message_id_cannot_escape(self):
        self.capture(entry_id="../../outside")
        self.assertTrue(self.directory(entry_id="../../outside").is_dir())
        self.assertEqual(self.capture(message="../outside"), "cache_unavailable")
        self.assertFalse((self.base / "outside").exists())

    def test_loose_cache_permissions_are_rejected(self):
        self.capture()
        self.directory().chmod(0o755)
        self.assertEqual(self.capture(), "cache_unavailable")
        self.assertEqual(self.described()[0][0]["status"], "unavailable")

    def test_missing_or_symlinked_copy_is_never_advertised_as_readable(self):
        self.capture()
        copy = Path(self.described()[0][0]["cached_path"])
        copy.unlink()
        self.assertNotIn("cached_path", self.described()[0][0])
        copy.symlink_to(self.source)
        self.assertNotIn("cached_path", self.described()[0][0])

    def test_invalid_manifest_paths_are_not_read_or_cleaned_outside_cache(self):
        self.capture()
        manifest = self.directory() / "manifest.json"
        data = json.loads(manifest.read_text())
        data["attachments"][0]["file"] = "../../../outside.png"
        manifest.write_text(json.dumps(data))
        self.assertNotIn("cached_path", self.described()[0][0])

    def test_manifest_symlink_is_rejected(self):
        self.capture()
        manifest = self.directory() / "manifest.json"
        manifest.unlink()
        manifest.symlink_to(self.source)
        self.assertEqual(self.capture(), "cache_unavailable")
        self.assertNotIn("cached_path", self.described()[0][0])

    def test_expiration_cleanup_is_throttled_and_only_removes_owned_namespace(self):
        with patch.object(attachments.time, "time", return_value=1000):
            self.capture()
        unrelated = self.image_root / "session" / "images"
        unrelated.mkdir(parents=True)
        image = unrelated / "native.png"
        image.write_bytes(b"native")
        with patch.object(attachments.time, "time", return_value=1000 + attachments.TTL):
            self.assertEqual(self.described()[0][0]["error"], "expired")
            attachments.cleanup()
            self.assertFalse(self.directory().exists())
            self.capture(message=2)
        self.assertEqual(image.read_bytes(), b"native")
        marker = self.image_root / "attachments" / ".last_cleanup"
        before = marker.stat().st_mtime
        with patch.object(attachments.time, "time", return_value=before + 1):
            attachments.cleanup()
        self.assertEqual(marker.stat().st_mtime, before)

    def test_cleanup_does_not_follow_symlinked_entry_or_message_or_copy(self):
        self.capture()
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "01.png"
        sentinel.write_bytes(b"keep")
        root = self.image_root / "attachments"
        (root / "linked-entry").symlink_to(outside, target_is_directory=True)
        (self.directory().parent / "99").symlink_to(outside, target_is_directory=True)
        (self.directory() / "02.png").symlink_to(sentinel)
        with patch.object(attachments.time, "time", return_value=__import__("time").time() + attachments.TTL + 1):
            attachments.cleanup()
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_cache_purge_does_not_recopy_original(self):
        self.capture()
        for p in self.directory().iterdir():
            p.unlink()
        items, _ = self.described()
        self.assertEqual(items[0]["status"], "unavailable")
        self.assertNotIn("cached_path", items[0])
        self.assertTrue(self.source.exists())


class RelayAttachmentTest(unittest.TestCase):
    relay = entry_tests.EntryTest.relay
    task = entry_tests.EntryTest.task

    def setUp(self):
        entry_tests.EntryTest.setUp(self)
        self.image_root = Path(self.tmp.name).resolve() / "claude-test" / "synthetic-home"
        self.path_patch = patch.object(paths, "orch_images", return_value=self.image_root)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.source = Path(self.tmp.name).resolve() / "測試 screenshot.png"
        self.source.write_bytes(b"synthetic-image")

    def tearDown(self):
        entry_tests.EntryTest.tearDown(self)

    def say(self, text=BODY, sources=None, **kw):
        self.rt.nat_says("desk-1", text, **kw)
        self.rt.user_messages["desk-1"][-1]["local_images"] = sources or [str(self.source)]

    def inbox(self):
        return entry.inbox(self.con, self.orch, self.rt)["messages"][0]

    def test_copy_precedes_wake_and_original_db_body_digest_are_unchanged(self):
        self.say()
        send = self.rt.send_uds
        def check_copy(*args):
            copies = list((self.image_root / "attachments").glob("*/*/01.png"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_bytes(), b"synthetic-image")
            return send(*args)
        with patch.object(self.rt, "send_uds", side_effect=check_copy):
            result = self.relay()
        self.source.unlink()
        msg = self.inbox()
        row = self.con.execute("SELECT * FROM entry_messages WHERE id=?", (result["message_id"],)).fetchone()
        self.assertEqual(row["body"], BODY)
        self.assertIsNone(row["source_raw"])
        self.assertEqual(msg["source_body_sha256"], result["body_sha256"])
        self.assertEqual(msg["source_body_bytes"], len(BODY.encode()))
        self.assertEqual(msg["body_sha256"], hashlib.sha256(msg["body"].encode()).hexdigest())
        self.assertEqual(msg["body_bytes"], len(msg["body"].encode()))
        self.assertTrue(msg["body"].startswith(BODY))
        self.assertIn(msg["attachments"][0]["cached_path"], msg["body"])

    def test_deleted_source_preserves_text_and_reports_copy_failure(self):
        self.say()
        self.source.unlink()
        self.assertEqual(self.relay()["status"], "delivered")
        msg = self.inbox()
        self.assertTrue(msg["body"].startswith(BODY))
        self.assertEqual(msg["attachments"][0]["error"], "source_missing")

    def test_duplicate_relay_does_not_recopy(self):
        self.say()
        with patch.object(attachments, "capture", wraps=attachments.capture) as capture:
            self.relay()
            self.source.unlink()
            self.assertEqual(self.relay()["status"], "duplicate")
            self.assertEqual(capture.call_count, 1)
        self.assertIn("cached_path", self.inbox()["attachments"][0])

    def test_concurrent_relay_captures_one_message_once(self):
        self.say()
        barrier = threading.Barrier(2)
        original = self.rt.codex_user_messages
        def source(thread):
            rows = original(thread)
            barrier.wait(timeout=10)
            return rows
        def relay():
            con = store.connect(Path(self.tmp.name) / "t.db")
            try:
                return entry.relay(con, self.rt, "desktop", "desk-1")
            finally:
                con.close()
        with patch.object(self.rt, "codex_user_messages", side_effect=source), \
                patch.object(attachments, "capture", wraps=attachments.capture) as capture, \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: relay(), range(2)))
        self.assertEqual({r["message_id"] for r in results}, {results[0]["message_id"]})
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM entry_messages").fetchone()[0], 1)

    def test_voice_keeps_source_raw_and_voice_body_digest(self):
        raw = "<realtime_delegation><input>看圖片</input><transcript_delta>user: 看圖片</transcript_delta></realtime_delegation>"
        self.say(raw)
        result = self.relay()
        msg = self.inbox()
        row = self.con.execute("SELECT * FROM entry_messages WHERE id=?", (result["message_id"],)).fetchone()
        self.assertEqual(row["source_raw"], raw)
        self.assertIn(entry.VOICE_LABEL, row["body"])
        self.assertEqual(msg["source_body_sha256"], row["body_sha256"])

    def test_reply_links_and_question_state_survive_attachment_copy(self):
        entry.touch(self.con, "desktop", "desk-1")
        question = entry.ask_nat(self.con, self.rt, self.orch, "which image?")["question_id"]
        self.say()
        self.relay(reply_to=question)
        self.assertEqual(self.inbox()["reply_to"], question)
        self.assertEqual(self.con.execute("SELECT question_state FROM entry_messages WHERE id=?", (question,)).fetchone()[0],
                         "answered")

    def test_ordinary_text_path_is_never_copied_or_added_to_inbox(self):
        self.rt.nat_says("desk-1", "look at " + str(self.source))
        self.relay()
        msg = self.inbox()
        self.assertNotIn("attachments", msg)
        self.assertNotIn("source_body_sha256", msg)
        self.assertFalse((self.image_root / "attachments").exists())

    def test_purged_cache_reports_missing_from_structured_rollout(self):
        self.say("no file wrapper")
        self.relay()
        for p in (self.image_root / "attachments").glob("*/*/*"):
            p.unlink()
        msg = self.inbox()
        self.assertEqual(msg["attachments"][0]["status"], "unavailable")
        self.assertNotIn("cached_path", msg["attachments"][0])

    def test_unread_expired_copy_is_removed_and_reported_unavailable(self):
        with patch.object(attachments.time, "time", return_value=1000):
            self.say()
            self.relay()
        with patch.object(attachments.time, "time", return_value=1000 + attachments.TTL + 1):
            msg = self.inbox()
        self.assertEqual(msg["attachments"][0]["status"], "unavailable")
        self.assertEqual(list((self.image_root / "attachments").glob("*/*/01.png")), [])

    def test_cache_failure_and_missing_rollout_still_deliver_original_text(self):
        self.say(f"# Files mentioned by the user:\n\n## example.png: {self.source}\nImage attachment: true\n\n## My request:\nsee this")
        with patch.object(attachments, "capture", return_value="cache_unavailable"):
            self.assertEqual(self.relay()["attachment_error"], "cache_unavailable")
        with patch.object(self.rt, "codex_user_messages", side_effect=OSError("rollout gone")):
            msg = self.inbox()
        self.assertEqual(msg["attachments"][0]["status"], "unavailable")
        self.assertTrue(self.source.exists())


class RolloutImageTest(unittest.TestCase):
    def test_structured_local_image_paths_survive_text_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-date-synthetic-thread.jsonl"
            content = [{"type": "text", "text": BODY}, {"type": "local_image", "path": "/synthetic/截圖 one.png"},
                       {"type": "local_image", "path": "/synthetic/two.png"}, {"type": "local_image", "path": 42},
                       {"type": "input_image", "image_url": "ignored"}]
            path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t",
                            "item": {"type": "UserMessage", "id": "i", "content": content}}}))
            with patch.dict(os.environ, {"ORCHD_CODEX_SESSIONS": tmp}):
                source, = Runtime().codex_user_messages("synthetic-thread")
            self.assertEqual(source["text"], BODY)
            self.assertEqual(source["local_images"], ["/synthetic/截圖 one.png", "/synthetic/two.png"])

    def test_cache_root_uses_uid_and_configured_orch_cwd(self):
        with patch.dict(os.environ, {"ORCHD_ORCH_HOME": "/synthetic/orch home"}), patch.object(os, "getuid", return_value=123):
            root = paths.orch_images()
        self.assertEqual(root, Path("/tmp").resolve() / "claude-123" / "-synthetic-orch-home")


if __name__ == "__main__":
    unittest.main()
