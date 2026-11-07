"""Opt-in smoke test of a packaged install without checkout imports or provider login."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("ORCHD_RUN_INSTALL_SMOKE") == "1", "opt-in packaged-install smoke")
class PackageInstallTests(unittest.TestCase):
    def test_blank_tool_install_initializes_a_restricted_interface(self):
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "the install smoke requires uv")
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="orchd-install-") as tmp:
            root = Path(tmp)
            home = root / "user"
            home.mkdir()
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONPATH", "PYTHONHOME")
                   and not k.startswith(("ORCHD_", "CODEX_", "CLAUDE_"))}
            env.update(UV_TOOL_DIR=str(root / "tools"), UV_TOOL_BIN_DIR=str(root / "bin"),
                       HOME=str(home), USERPROFILE=str(home), PYTHONUTF8="1",
                       ORCHD_HOME=str(root / "state"), ORCHD_ROOT=str(home / "orch"),
                       ORCHD_CONFIG_DIR=str(home / "settings"), CODEX_HOME=str(home / ".codex"))
            install = subprocess.run([uv, "tool", "install", "--python", sys.executable,
                                      "--from", str(source), "orchd"], env=env,
                                     cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=180)
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            executable = root / "bin" / ("orchd.exe" if os.name == "nt" else "orchd")
            self.assertTrue(executable.is_file())

            def command(*args):
                result = subprocess.run([str(executable), *args], cwd=root, env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            self.assertIn("dispatch", command("--help"))
            self.assertIn("--instructions", command("dispatch", "--help"))
            result = json.loads(command("init", "--no-trust"))
            interface = Path(result["interface"])
            self.assertEqual(interface, home / "orch" / "interface")
            cfg = tomllib.loads((interface / ".codex" / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(cfg["model"], "gpt-6.1-terra")
            self.assertEqual(cfg["default_permissions"], "interface")
            fs = cfg["permissions"]["interface"]["filesystem"]
            self.assertEqual(fs[str(home)], "deny")
            self.assertEqual(fs[str(interface)], "read")
            self.assertEqual(fs[":minimal"], "read")
            self.assertNotIn(":root", fs)
            self.assertFalse(cfg["permissions"]["interface"]["network"]["enabled"])
            for name in ("shell_tool", "unified_exec", "plugins", "apps", "browser_use", "computer_use"):
                self.assertFalse(cfg["features"][name], name)
            server = cfg["mcp_servers"]["orchd_entry"]
            self.assertEqual(set(server["tools"]), {"status", "relay", "foreground"})
            self.assertTrue(Path(server["command"]).parent.resolve().is_relative_to((root / "tools").resolve()),
                            f"unexpected MCP command {server['command']} for tool root {root}")
            self.assertNotIn(str(source), server["command"])
            if os.name == "nt":
                self.assertEqual(Path(server["command"]).name, "orchd.exe")
                self.assertEqual(server["args"], ["mcp", "--role", "entry"])
                self.assertEqual(cfg["windows"]["sandbox"], "elevated")
                self.assertEqual(cfg["project_doc_max_bytes"], 0)
                self.assertIn("ALL_TOOLS", cfg["developer_instructions"])
            requests = "\n".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
                                 for i, method in ((1, "initialize"), (2, "tools/list"))) + "\n"
            mcp = subprocess.run([server["command"], *server["args"]], input=requests,
                                 env=dict(env, **server.get("env", {})), cwd=root,
                                 capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(mcp.returncode, 0, mcp.stdout + mcp.stderr)
            responses = [json.loads(line) for line in mcp.stdout.splitlines() if line.strip()]
            inventory = next(item["result"]["tools"] for item in responses if item.get("id") == 2)
            self.assertEqual({tool["name"] for tool in inventory}, {"status", "relay", "foreground"})
            self.assertFalse((home / ".codex" / "config.toml").exists())
            self.assertEqual(set(json.loads(command("init", "--no-trust"))["files"].values()), {"unchanged"})
