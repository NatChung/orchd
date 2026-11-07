"""Windows upgrades preserve live imports and switch new invocations atomically."""
import json
import shutil
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchd import upgrade


@unittest.skipUnless(os.name == "nt", "Windows tool installations")
class WindowsUpgradeTests(unittest.TestCase):
    def make_prefix(self, root, label):
        prefix = root / label / "tools" / "orchd"
        scripts = prefix / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").touch()
        (scripts / "orchd.exe").touch()
        (scripts / "orchd-flutter-gate.exe").touch()
        return prefix

    def test_staging_failure_does_not_change_current_or_stable_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            anchor = self.make_prefix(root, "old")
            candidate = self.make_prefix(anchor.parent / ".orchd-versions", "new")
            stable_bin = root / "bin"
            stable_bin.mkdir()
            launcher = stable_bin / "orchd.exe"
            launcher.write_bytes(b"stable launcher")
            upgrade.publish_windows_install(anchor, candidate, stable_bin)
            pointer = anchor / "orchd-current.json"
            before_pointer = pointer.read_bytes()

            def fail_install(command, *, env=None):
                return subprocess.CompletedProcess(command, 1, "", "synthetic install failure")

            code, messages = upgrade.windows_upgrade(
                candidate, "invalid-source", run=fail_install, which=lambda _: "uv"
            )
            self.assertNotEqual(code, 0)
            self.assertIn("synthetic install failure", "\n".join(messages))
            self.assertEqual(pointer.read_bytes(), before_pointer)
            self.assertEqual(launcher.read_bytes(), b"stable launcher")

    def test_stale_publication_cannot_replace_newer_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            anchor = self.make_prefix(root, "old")
            versions = anchor.parent / ".orchd-versions"
            first = self.make_prefix(versions, "first")
            newer = self.make_prefix(versions, "newer")
            stale = self.make_prefix(versions, "stale")
            stable_bin = root / "bin"
            stable_bin.mkdir()
            upgrade.publish_windows_install(anchor, first, stable_bin)
            upgrade.publish_windows_install(anchor, newer, stable_bin, expected_current=first)
            with self.assertRaisesRegex(ValueError, "current|changed|stale"):
                upgrade.publish_windows_install(anchor, stale, stable_bin, expected_current=first)
            pointer = json.loads((anchor / "orchd-current.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(pointer["prefix"]), newer)

    def test_live_process_retains_old_lazy_import_after_new_install_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            anchor = self.make_prefix(root, "old")
            candidate = self.make_prefix(anchor.parent / ".orchd-versions", "new")
            stable_bin = root / "bin"
            stable_bin.mkdir()
            old_package = anchor / "Lib" / "site-packages" / "isolation_fixture"
            new_package = candidate / "Lib" / "site-packages" / "isolation_fixture"
            for package, version in ((old_package, "old"), (new_package, "new")):
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("", encoding="utf-8")
                (package / "lazy.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")

            script = """
import sys
sys.path.insert(0, sys.argv[1])
import isolation_fixture
print('ready', flush=True)
sys.stdin.readline()
from isolation_fixture import lazy
print(lazy.VERSION, flush=True)
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(old_package.parent)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                upgrade.publish_windows_install(anchor, candidate, stable_bin)
                pointer = json.loads((anchor / "orchd-current.json").read_text(encoding="utf-8"))
                self.assertEqual(Path(pointer["prefix"]), candidate)
                process.stdin.write("continue\n")
                process.stdin.flush()
                self.assertEqual(process.stdout.readline().strip(), "old")
                self.assertEqual(process.wait(timeout=10), 0, process.stderr.read())
                fresh = subprocess.run(
                    [sys.executable, "-c", "import isolation_fixture.lazy as m; print(m.VERSION)"],
                    env={**os.environ, "PYTHONPATH": str(new_package.parent)},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(fresh.returncode, 0, fresh.stderr)
                self.assertEqual(fresh.stdout.strip(), "new")
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)


@unittest.skipUnless(os.name == "nt" and os.environ.get("ORCHD_RUN_INSTALL_SMOKE") == "1",
                     "opt-in packaged Windows upgrade")
class WindowsPackagedUpgradeTests(unittest.TestCase):
    def test_uv_upgrade_preserves_running_install_and_stable_launcher(self):
        uv = shutil.which("uv")
        if not uv:
            self.skipTest("uv is required for the packaged install smoke test")
        checkout = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="orch-upgrade-") as tmp:
            root = Path(tmp).resolve()
            tool_dir = root / "tools"
            bin_dir = root / "bin"
            env = dict(os.environ, UV_TOOL_DIR=str(tool_dir), UV_TOOL_BIN_DIR=str(bin_dir),
                       UV_CACHE_DIR=str(root / "cache"), UV_LINK_MODE="copy", ORCHD_ROOT=str(root / "orch"),
                       ORCHD_HOME=str(root / "state"), ORCHD_CONFIG_DIR=str(root / "config"),
                       HOME=str(root / "home"), USERPROFILE=str(root / "home"),
                       LOCALAPPDATA=str(root / "local"), APPDATA=str(root / "appdata"),
                       CODEX_HOME=str(root / "codex"), PYTHONUTF8="1")
            install = subprocess.run([uv, "tool", "install", str(checkout)], env=env,
                                     capture_output=True, text=True, timeout=180, cwd=root)
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            launcher = bin_dir / "orchd.exe"
            old_prefix = tool_dir / "orchd"
            old_python = old_prefix / "Scripts" / "python.exe"
            self.assertTrue(launcher.is_file())
            self.assertTrue(old_python.is_file())
            fixture = old_prefix / "Lib" / "site-packages" / "orchd" / "windows_pipe.py"
            with fixture.open("a", encoding="utf-8") as stream:
                stream.write("\n_UPGRADE_TEST_GENERATION = 'old'\n")
            script = """
import orchd
print('ready', flush=True)
input()
from orchd import windows_pipe
print(windows_pipe._UPGRADE_TEST_GENERATION, flush=True)
"""
            process = subprocess.Popen([str(old_python), "-c", script], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=root)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                updated = subprocess.run([str(launcher), "upgrade"], env=env, capture_output=True,
                                         text=True, timeout=180, cwd=root)
                self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
                self.assertTrue(launcher.is_file())
                pointer = json.loads((old_prefix / "orchd-current.json").read_text(encoding="utf-8"))
                active = Path(pointer["prefix"])
                self.assertNotEqual(active, old_prefix)
                self.assertTrue((active / "Scripts" / "orchd.exe").is_file())
                # Only the new generation contains this marker. Running the stable
                # entry point must load its CLI rather than the anchor's old CLI.
                active_cli = active / "Lib" / "site-packages" / "orchd" / "cli.py"
                with active_cli.open("a", encoding="utf-8") as stream:
                    stream.write("\nprint('UPGRADE-NEW-GENERATION')\n")
                check = subprocess.run([str(launcher), "--help"], env=env, capture_output=True,
                                       text=True, timeout=30, cwd=root)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
                self.assertIn("UPGRADE-NEW-GENERATION", check.stdout)
                process.stdin.write("continue\n")
                process.stdin.flush()
                self.assertEqual(process.stdout.readline().strip(), "old")
                self.assertEqual(process.wait(timeout=10), 0, process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
