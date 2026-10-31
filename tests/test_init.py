"""Issue #50: `orchd init` creates ~/orch/home and ~/orch/interface, trusts them in Codex, migrates an old home."""
import sys
import tempfile
import unittest
from pathlib import Path
from orchd import home_migrations

from orchd import bootstrap, mcp_server, paths

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None


class TrustRuntime:
    def __init__(self, trusted=False):
        self.trusted, self.checked = trusted, []

    def claude_trusted(self, path):
        self.checked.append(path)
        return self.trusted


@unittest.skipIf(tomllib is None, "needs Python 3.11+ tomllib")
class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "example"
        self.home.mkdir()
        self.env = {}
        self.rt = TrustRuntime()
        self.orchd_bin = str(Path(self.tmp.name) / "bin" / "orchd")
        Path(self.orchd_bin).parent.mkdir()
        Path(self.orchd_bin).write_text("#!/usr/bin/env python3\n")

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, **kw):
        return bootstrap.init(self.rt, home=self.home, env=self.env, orchd_bin=self.orchd_bin, **kw)

    def codex(self):
        return tomllib.loads((self.home / ".codex" / "config.toml").read_text())

    def test_fresh_home_gets_both_folders_trust_and_a_claude_hint(self):
        result = self.init()
        orch, interface = self.home / "orch" / "home", self.home / "orch" / "interface"
        self.assertEqual((Path(result["orch_home"]), Path(result["interface"])), (orch, interface))
        self.assertEqual(set(result["files"].values()), {"created"})
        for path in ("AGENTS.md", "PROJECTS.md", "handoffs/INDEX.md", ".codex/config.toml"):
            self.assertTrue((orch / path).is_file(), path)
        self.assertTrue((orch / "groups").is_dir())
        self.assertTrue((interface / "AGENTS.md").is_file())
        projects = self.codex()["projects"]
        self.assertEqual({k: v["trust_level"] for k, v in projects.items()},
                         {str(orch.resolve()): "trusted", str(interface.resolve()): "trusted"})
        self.assertIn("claude", result["claude_trust"])
        self.assertEqual(self.rt.checked, [orch.resolve()])

    def test_second_run_changes_nothing_and_adds_no_duplicate_trust(self):
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text('model = "gpt-6.1-sol"\n')
        first = self.init()
        self.assertTrue(all(v.startswith("written (backup ") for v in first["codex_trust"].values()))
        backups = list(cfg.parent.glob("config.toml.orchd-init-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'model = "gpt-6.1-sol"\n')
        after = cfg.read_text()
        second = self.init()
        self.assertEqual(set(second["files"].values()), {"unchanged"})
        self.assertEqual(set(second["codex_trust"].values()), {"already trusted"})
        self.assertEqual(cfg.read_text(), after)
        self.assertEqual(len(list(cfg.parent.glob("config.toml.orchd-init-*"))), 1)
        self.assertEqual(self.codex()["model"], "gpt-6.1-sol")

    def test_computer_use_home_rules_update_preserves_user_content_and_evidence(self):
        result = self.init(trust=False)
        worker = self.home / "orch/gpt-computer-use-worker"
        self.assertEqual(result["computer_use_worker"], str(worker))
        self.assertTrue((worker / "reports").is_dir())
        evidence = worker / "reports/step.png"
        evidence.write_bytes(b"evidence")
        rules = worker / "AGENTS.md"
        prefix, suffix = b"local before\r\n", b"\r\nlocal after"
        old = prefix + b"<!-- orchd:managed:start v=0 -->\nold\n<!-- orchd:managed:end -->" + suffix
        rules.write_bytes(old)
        updated = self.init(trust=False)
        self.assertEqual(updated["files"][str(rules)], "updated")
        template = (bootstrap.TEMPLATES / "gpt-computer-use-worker/AGENTS.md").read_bytes().rstrip(b"\n")
        self.assertEqual(rules.read_bytes(), prefix + template + suffix)
        backups = home_migrations.state_dir(self.home, self.env) / "backups/gpt-computer-use-worker"
        self.assertEqual(next(backups.iterdir()).read_bytes(), old)
        self.assertEqual(self.init(trust=False)["files"][str(rules)], "unchanged")
        self.assertEqual(len(list(backups.iterdir())), 1)
        self.assertEqual(evidence.read_bytes(), b"evidence")

    def test_computer_use_home_adopts_existing_rules_and_follows_root(self):
        self.env = {"ORCHD_ROOT": str(self.home / "custom")}
        worker = self.home / "custom/gpt-computer-use-worker"
        worker.mkdir(parents=True)
        rules = worker / "AGENTS.md"
        rules.write_bytes(b"my rules\r\n")
        self.init(trust=False)
        self.assertTrue(rules.read_bytes().startswith(b"my rules\r\n"))
        self.assertEqual(rules.read_bytes().count(b"orchd:managed:start"), 1)
        con = home_migrations.connect(self.home, self.env, readonly=True)
        self.addCleanup(con.close)
        self.assertEqual(home_migrations.targets(self.home, {}, con)["gpt-computer-use-worker"], worker)

    def test_edited_files_are_kept(self):
        self.init()
        agents = self.home / "orch" / "home" / "AGENTS.md"
        agents.write_text("my own rules\n")
        result = self.init()
        self.assertTrue(result["files"][str(agents)].startswith("kept"))
        self.assertEqual(agents.read_text(), "my own rules\n")

    def test_no_trust_touches_no_codex_config(self):
        result = self.init(trust=False)
        self.assertNotIn("codex_trust", result)
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(self.rt.checked, [])

    def test_unparseable_codex_config_is_left_alone(self):
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text("not = = toml\n")
        result = self.init()
        self.assertTrue(all(v.startswith("skipped") for v in result["codex_trust"].values()))
        self.assertEqual(cfg.read_text(), "not = = toml\n")

    def test_configs_lock_the_folders_and_preapprove_every_tool(self):
        self.init(trust=False)
        orch, interface = self.home / "orch" / "home", self.home / "orch" / "interface"
        home_cfg = tomllib.loads((orch / ".codex" / "config.toml").read_text())
        fs = home_cfg["permissions"]["orch"]["filesystem"]
        self.assertEqual((fs[str(self.home)], fs[str(orch)]), ("deny", "write"))
        self.assertEqual(home_cfg["mcp_servers"]["orchd"]["command"], sys.executable)
        self.assertEqual(home_cfg["mcp_servers"]["orchd"]["args"], [self.orchd_bin, "mcp"])
        self.assertEqual(set(home_cfg["mcp_servers"]["orchd"]["tools"]), {t["name"] for t in mcp_server.TOOLS})
        iface = tomllib.loads((interface / ".codex" / "config.toml").read_text())
        self.assertEqual(iface["model"], "gpt-6.1-terra")
        self.assertEqual(iface["model_reasoning_effort"], "low")
        fs = iface["permissions"]["interface"]["filesystem"]
        self.assertEqual((fs[str(self.home)], fs[str(interface)]), ("deny", "read"))
        self.assertFalse(iface["permissions"]["interface"]["network"]["enabled"])
        self.assertEqual(iface["web_search"], "disabled")  # web search ignores network.enabled (#55)
        for feature in ("shell_tool", "unified_exec", "image_generation", "view_image", "goals", "multi_agent"):
            self.assertIs(iface["features"][feature], False, feature)
        self.assertEqual(iface["mcp_servers"]["orchd_entry"]["command"], sys.executable)
        self.assertEqual(iface["mcp_servers"]["orchd_entry"]["args"], [self.orchd_bin, "mcp", "--role", "entry"])
        self.assertNotIn("/usr/bin/python3", (interface / ".codex" / "config.toml").read_text())
        self.assertEqual({k: v["approval_mode"] for k, v in iface["mcp_servers"]["orchd_entry"]["tools"].items()},
                         {"relay": "approve", "status": "approve", "foreground": "approve"})

    def test_generic_template_has_no_personal_values(self):
        self.init(trust=False)
        text = (self.home / "orch" / "home" / ".codex" / "config.toml").read_text()
        for personal in ("mattpocock", "connector_", "RTK.md", "guidance", "ExampleOwner", "ExampleOrg"):
            self.assertNotIn(personal, text)
        fs = tomllib.loads(text)["permissions"]["orch"]["filesystem"]
        self.assertEqual(set(fs), {":minimal", str(self.home), f"{self.home}/.local/bin",
                                   f"{self.home}/.codex/packages/standalone", "/opt/homebrew",
                                   str(self.home / "orch" / "home"), str(self.home / "orch" / "home" / ".codex")})

    def test_personal_init_toml_adds_reads_and_disabled_apps(self):
        config = self.home / ".config" / "orchd"
        config.mkdir(parents=True)
        (config / "init.toml").write_text('[home]\nread = ["~/AGENTS.md", "/opt/extra dir"]\n'
                                          'disabled_apps = ["example-app"]\n')
        self.init(trust=False)
        cfg = tomllib.loads((self.home / "orch" / "home" / ".codex" / "config.toml").read_text())
        fs = cfg["permissions"]["orch"]["filesystem"]
        self.assertEqual((fs[f"{self.home}/AGENTS.md"], fs["/opt/extra dir"]), ("read", "read"))
        self.assertEqual(cfg["apps"], {"example-app": {"enabled": False}})
        (config / "init.toml").write_text('[home]\nread = [""]\n')
        with self.assertRaisesRegex(ValueError, "non-empty strings"):
            self.init(trust=False)

    def test_non_default_state_dir_reaches_both_mcp_servers(self):
        self.env = {"ORCHD_HOME": "/tmp/orchd-state"}
        self.init(trust=False)
        for folder, server in (("home", "orchd"), ("interface", "orchd_entry")):
            cfg = tomllib.loads((self.home / "orch" / folder / ".codex" / "config.toml").read_text())
            self.assertEqual(cfg["mcp_servers"][server]["env"], {"ORCHD_HOME": "/tmp/orchd-state"})

    def test_projects_override_reaches_both_mcp_servers(self):
        self.env = {"ORCHD_PROJECTS": "~/Git Projects", "ORCHD_CONFIG_DIR": str(self.home / "settings")}
        self.init(trust=False)
        for folder, server in (("home", "orchd"), ("interface", "orchd_entry")):
            cfg = tomllib.loads((self.home / "orch" / folder / ".codex" / "config.toml").read_text())
            self.assertEqual(cfg["mcp_servers"][server]["env"], {
                "ORCHD_PROJECTS": str(self.home / "Git Projects"),
                "ORCHD_CONFIG_DIR": str(self.home / "settings"),
            })

    def test_migrate_copies_kept_files_and_skips_generated_and_vcs(self):
        old = Path(self.tmp.name) / "old-orch"
        for path, text in {"AGENTS.md": "old rules", ".codex/config.toml": "old", ".git/HEAD": "ref", ".gitignore": "x",
                           ".DS_Store": "x", ".claude/worktrees/a": "x", "PROJECTS.md": "my projects",
                           "groups/o1.md": "notes", "handoffs/2026-09-30.md": "wrapup",
                           "handoffs/codex-projects/a.md": "deep"}.items():
            (old / path).parent.mkdir(parents=True, exist_ok=True)
            (old / path).write_text(text)
        result = self.init(trust=False, source=old)
        orch = self.home / "orch" / "home"
        self.assertEqual(sorted(result["migrated"]["copied"]),
                         ["groups/o1.md", "handoffs/2026-09-30.md", "handoffs/codex-projects/a.md"])
        self.assertEqual(result["migrated"]["replaced_init_stub"], ["PROJECTS.md"])  # the old content replaces the stub
        self.assertEqual((orch / "PROJECTS.md").read_text(), "my projects")
        # a file Operator already edited in the new home is never overwritten
        (orch / "handoffs" / "INDEX.md").write_text("edited in the new home")
        (old / "handoffs" / "INDEX.md").write_text("old index")
        again = self.init(trust=False, source=old)
        self.assertEqual(again["migrated"]["kept_existing"], ["handoffs/INDEX.md"])
        self.assertEqual((orch / "handoffs" / "INDEX.md").read_text(), "edited in the new home")
        self.assertNotEqual((orch / "AGENTS.md").read_text(), "old rules")
        self.assertFalse((orch / ".git").exists())
        self.assertEqual((old / "groups" / "o1.md").read_text(), "notes")  # source untouched

    def test_codex_templates_launch_mcp_under_the_orchd_interpreter(self):
        # bin/orchd's shebang is the system python3, which may lack psutil (#89, same crash as #88).
        self.init()
        orch, interface = self.home / "orch" / "home", self.home / "orch" / "interface"
        for folder, server, tail in ((orch, "orchd", ["mcp"]), (interface, "orchd_entry", ["mcp", "--role", "entry"])):
            spec = tomllib.loads((folder / ".codex" / "config.toml").read_text())["mcp_servers"][server]
            self.assertEqual((spec["command"], spec["args"]), (sys.executable, [self.orchd_bin, *tail]), server)

    def test_non_python_orchd_override_is_launched_directly(self):
        script = Path(self.tmp.name) / "wrapper"
        script.write_text("#!/bin/sh\nexec true\n")
        files = bootstrap.planned_files(self.home, {"ORCHD_EXECUTABLE": str(script)})
        spec = tomllib.loads(files[self.home / "orch" / "home" / ".codex" / "config.toml"])["mcp_servers"]["orchd"]
        self.assertEqual((spec["command"], spec["args"]), (str(script), ["mcp"]))

    def test_native_orchd_override_is_launched_directly(self):
        # /bin/echo has no shebang; running it under python is a SyntaxError (rc 1).
        echo = next(p for p in ("/bin/echo", "/usr/bin/echo") if Path(p).is_file())
        self.assertEqual(paths.mcp_launch(echo), (echo, []))
        files = bootstrap.planned_files(self.home, {"ORCHD_EXECUTABLE": echo})
        spec = tomllib.loads(files[self.home / "orch" / "home" / ".codex" / "config.toml"])["mcp_servers"]["orchd"]
        self.assertEqual((spec["command"], spec["args"]), (echo, ["mcp"]))

    def test_unclassifiable_override_falls_back_to_direct_launch(self):
        d = Path(self.tmp.name)
        (d / "plain").write_text("not a script\n")
        for target in (d / "missing", d / "plain"):
            self.assertEqual(paths.mcp_launch(str(target)), (str(target), []), target.name)
        (d / "tool.py").write_text("print(1)\n")
        self.assertEqual(paths.mcp_launch(str(d / "tool.py")), (sys.executable, [str(d / "tool.py")]))

    def test_paths_follow_overrides(self):
        self.assertEqual(paths.orch_home(self.home, {}), self.home / "orch" / "home")
        self.assertEqual(paths.interface_home(self.home, {"ORCHD_ROOT": "/x"}), Path("/x/interface"))
        self.assertEqual(paths.orch_home(self.home, {"ORCHD_ORCH_HOME": "/y"}), Path("/y"))


if __name__ == "__main__":
    unittest.main()
