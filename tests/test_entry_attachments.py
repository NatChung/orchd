import concurrent.futures
import base64
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
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.image_root = self.base / "claude-test" / "synthetic-home"
        self.path_patch = patch.object(paths, "orch_images", return_value=self.image_root)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.source_root = self.base / "desktop-temp"
        folder = self.source_root / "TemporaryItems" / "NSIRD_screencaptureui_ABC123"
        folder.mkdir(parents=True)
        self.root_patch = patch.object(attachments, "_desktop_temp_root", return_value=self.source_root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.source = folder / "截圖 with spaces.png"
        self.source.write_bytes(PNG)

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
        missing = self.source.parent / "missing.png"
        with patch.object(attachments, "MAX_BYTES", 4):
            self.capture([str(missing), str(self.source)])
        items, _ = self.described()
        self.assertEqual([i["error"] for i in items], ["source_missing", "too_large"])
        self.assertEqual([p.name for p in self.directory().iterdir()], ["manifest.json"])

    def test_source_growth_is_bounded_even_when_initial_stat_is_small(self):
        self.source.write_bytes(PNG)
        real_stat = os.fstat
        def smaller(fd):
            s = real_stat(fd)
            if s.st_ino == self.source.stat().st_ino:
                fields = list(s)
                fields[6] = 1
                return os.stat_result(fields)
            return s
        with patch.object(attachments, "MAX_BYTES", 12), patch.object(attachments.os, "fstat", side_effect=smaller):
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
        self.assertEqual(self.source.read_bytes(), PNG)

    def test_rejects_fifo_without_blocking(self):
        fifo = self.source.parent / "input.png"
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

    def test_private_key_text_is_rejected_inside_and_outside_allowlist_without_content_in_note(self):
        secret = b"-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETIC-DO-NOT-COPY\n"
        outside = self.base / ".ssh" / "id_ed25519"
        outside.parent.mkdir()
        outside.write_bytes(secret)
        disguised = self.source.parent / "key.png"
        disguised.write_bytes(secret)
        self.capture([str(outside), str(disguised)])
        items, _ = self.described()
        self.assertEqual([i["error"] for i in items], ["source_outside_allowlist", "not_an_image"])
        self.assertEqual([p.name for p in self.directory().iterdir()], ["manifest.json"])
        self.assertNotIn("SYNTHETIC-DO-NOT-COPY", (self.directory() / "manifest.json").read_text())

    def test_extension_mismatch_and_unsupported_extension_are_not_copied(self):
        files = [("jpeg.png", b"\xff\xd8\xff\xe0fake-jpeg"), ("png.jpg", PNG),
                 ("image.txt", PNG), ("id_ed25519", PNG)]
        sources = []
        for name, content in files:
            file = self.source.parent / name
            file.write_bytes(content)
            sources.append(str(file))
        self.capture(sources)
        self.assertEqual([i["error"] for i in self.described()[0]],
                         ["image_extension_mismatch", "image_extension_mismatch", "unsupported_extension", "unsupported_extension"])
        self.assertEqual([p.name for p in self.directory().iterdir()], ["manifest.json"])

    def test_only_observed_screenshot_temp_pattern_is_allowed(self):
        relative_paths = ["anything.png", "Codex/image.png", "TemporaryItems/Other/image.png",
                          "TemporaryItems/NSIRD_screencaptureui_SHORT/image.png",
                          "TemporaryItems/NSIRD_screencaptureui_ABC123/nested/image.png"]
        for relative in relative_paths:
            with self.subTest(relative=relative):
                self.assertFalse(attachments._allowed_source(self.source_root / relative))
        self.assertFalse(attachments._allowed_source(Path(str(self.source_root) + "-other") /
                                                     "TemporaryItems/NSIRD_screencaptureui_ABC123/image.png"))
        self.assertTrue(attachments._allowed_source(self.source))

    def test_even_a_real_png_outside_allowlist_is_rejected_before_open(self):
        outside = self.base / "outside.png"
        outside.write_bytes(PNG)
        with patch.object(attachments.os, "open", wraps=os.open) as opened:
            self.capture([str(outside)])
        self.assertNotIn(outside.name, [c.args[0] for c in opened.call_args_list])
        self.assertEqual(self.described()[0][0]["error"], "source_outside_allowlist")

    def test_symlink_in_allowlist_cannot_point_to_an_outside_image(self):
        outside = self.base / "outside.png"
        outside.write_bytes(PNG)
        direct = self.source.parent / "link.png"
        direct.symlink_to(outside)
        parent_link = self.source_root / "TemporaryItems" / "NSIRD_screencaptureui_LINK12"
        parent_link.symlink_to(self.base, target_is_directory=True)
        self.capture([str(direct), str(parent_link / "outside.png")])
        self.assertTrue(all(i["status"] == "failed" for i in self.described()[0]))
        self.assertEqual([p.name for p in self.directory().iterdir()], ["manifest.json"])

    def test_post_open_fd_path_is_checked_before_any_content_read(self):
        with patch.object(attachments, "_fd_realpath", return_value=self.base / "outside.png"), \
                patch.object(attachments.os, "read", wraps=os.read) as read:
            self.capture()
        read.assert_not_called()
        self.assertEqual(self.described()[0][0]["error"], "source_outside_allowlist")

    def test_changed_or_unverifiable_fd_path_fails_closed(self):
        other = self.source.parent / "changed.png"
        with patch.object(attachments, "_fd_realpath", return_value=other):
            self.capture()
        self.assertEqual(self.described()[0][0]["error"], "source_path_changed")
        with patch.object(attachments, "_fd_realpath", side_effect=ValueError("source_path_unverifiable")):
            self.capture(message=2)
        self.assertEqual(self.described(message=2)[0][0]["error"], "source_path_unverifiable")

    def test_hard_link_to_an_outside_image_is_rejected(self):
        outside = self.base / "outside.png"
        outside.write_bytes(PNG)
        linked = self.source.parent / "linked.png"
        os.link(outside, linked)
        self.capture([str(linked)])
        self.assertEqual(self.described()[0][0]["error"], "unsafe_source")

    def test_known_magic_headers_match_the_allowed_extensions(self):
        def ftyp(major, compatible):
            payload = major + b"\0" * 4 + compatible
            return (len(payload) + 8).to_bytes(4, "big") + b"ftyp" + payload
        samples = [(".png", PNG), (".jpg", b"\xff\xd8\xff\xe0"), (".jpeg", b"\xff\xd8\xff\xe1"),
                   (".gif", b"GIF87a"), (".gif", b"GIF89a"),
                   (".webp", b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 "),
                   (".heic", ftyp(b"heic", b"mif1")), (".heic", ftyp(b"mif1", b"heic")),
                   (".heif", ftyp(b"heic", b"mif1")), (".heif", ftyp(b"mif1", b"mif1"))]
        for i, (suffix, content) in enumerate(samples, 1):
            with self.subTest(suffix=suffix, content=content[:16]):
                image = self.source.parent / ("image" + suffix)
                image.write_bytes(content)
                self.capture([str(image)], message=i)
                result = self.described(message=i)[0][0]
                self.assertEqual(Path(result["cached_path"]).read_bytes(), content)

    def test_truncated_unknown_and_non_heif_ftyp_headers_are_rejected(self):
        headers = [b"\x89PNG", b"plain text", b"\0\0\0\x18ftypmif1\0\0\0\0",
                   b"\0\0\0\x14ftypmp42\0\0\0\0heic", b"\0\0\0\x14ftypmif1\0\0\0\0avif"]
        for header in headers:
            self.assertIsNone(attachments._image_kind(header))
        with self.assertRaisesRegex(ValueError, "image_extension_mismatch"):
            attachments._validate_image(b"\0\0\0\x14ftypmif1\0\0\0\0mif1", ".heic")

    def test_legacy_unvalidated_manifest_is_not_advertised(self):
        self.capture()
        manifest = self.directory() / "manifest.json"
        data = json.loads(manifest.read_text())
        data["version"] = 1
        manifest.write_text(json.dumps(data))
        self.assertNotIn("cached_path", self.described()[0][0])


class RelayAttachmentTest(unittest.TestCase):
    relay = entry_tests.EntryTest.relay
    task = entry_tests.EntryTest.task

    def setUp(self):
        entry_tests.EntryTest.setUp(self)
        self.image_root = Path(self.tmp.name).resolve() / "claude-test" / "synthetic-home"
        self.path_patch = patch.object(paths, "orch_images", return_value=self.image_root)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.source_root = Path(self.tmp.name).resolve() / "desktop-temp"
        folder = self.source_root / "TemporaryItems" / "NSIRD_screencaptureui_ABC123"
        folder.mkdir(parents=True)
        self.root_patch = patch.object(attachments, "_desktop_temp_root", return_value=self.source_root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.source = folder / "測試 screenshot.png"
        self.source.write_bytes(PNG)

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
            self.assertEqual(copies[0].read_bytes(), PNG)
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
    def test_desktop_temp_root_comes_from_os_not_tmpdir(self):
        import subprocess
        result = subprocess.CompletedProcess([], 0, stdout="/private/var/folders/ab/synthetic_user/T/\n")
        with patch.object(attachments.sys, "platform", "darwin"), \
                patch.object(attachments.subprocess, "run", return_value=result) as run, \
                patch.dict(os.environ, {"TMPDIR": "/untrusted"}):
            root = attachments._desktop_temp_root.__wrapped__()
        self.assertEqual(root, Path("/private/var/folders/ab/synthetic_user/T"))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"])

    def test_unknown_os_temp_root_fails_closed(self):
        import subprocess
        result = subprocess.CompletedProcess([], 0, stdout="/Users/synthetic/Downloads\n")
        with patch.object(attachments.sys, "platform", "darwin"), patch.object(attachments.subprocess, "run", return_value=result):
            self.assertIsNone(attachments._desktop_temp_root.__wrapped__())

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
