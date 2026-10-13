import contextlib
import gc
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import cli, config, core, store
from tests.test_orchd import FakeRuntime


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "cfg"
        env = {k: v for k, v in os.environ.items() if k not in ("ORCHD_WORKER_MODEL", "ORCHD_CONFIG_FILE")}
        env.update(ORCHD_CONFIG_DIR=str(self.dir), ORCHD_HOME=self.tmp.name)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.file = self.dir / "config.toml"

    def write(self, text):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file.write_text(text)

    def run_cli(self, *argv):
        gc.collect()  # connections leaked by other tests must not warn into the stderr captured below
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # resolution order: explicit > env > file > default
    def test_missing_file_uses_factory_default(self):
        self.assertFalse(self.file.exists())
        self.assertEqual(config.worker_model(), ("sonnet", "default"))

    def test_file_beats_default(self):
        self.write('[worker]\nmodel = "sol"\n')
        self.assertEqual(config.worker_model(), ("sol", "file"))

    def test_env_beats_file(self):
        self.write('[worker]\nmodel = "sol"\n')
        with mock.patch.dict(os.environ, {"ORCHD_WORKER_MODEL": "sonnet"}):
            self.assertEqual(config.worker_model(), ("sonnet", "env"))

    def test_explicit_beats_env_and_file(self):
        self.write('[worker]\nmodel = "sol"\n')
        with mock.patch.dict(os.environ, {"ORCHD_WORKER_MODEL": "sol"}):
            self.assertEqual(config.worker_model("sonnet"), ("sonnet", "flag"))

    def test_empty_env_is_unset(self):
        with mock.patch.dict(os.environ, {"ORCHD_WORKER_MODEL": ""}):
            self.assertEqual(config.worker_model(), ("sonnet", "default"))

    # fail closed
    def test_invalid_values_raise_naming_the_source(self):
        with mock.patch.dict(os.environ, {"ORCHD_WORKER_MODEL": "opus"}):
            with self.assertRaisesRegex(ValueError, "ORCHD_WORKER_MODEL.*opus.*sol, sonnet"):
                config.worker_model()
        self.write('[worker]\nmodel = "opus"\n')
        with self.assertRaisesRegex(ValueError, r"config\.toml.*opus.*sol, sonnet"):
            config.worker_model()
        with self.assertRaisesRegex(ValueError, "flag.*opus"):
            config.worker_model("opus")

    def test_malformed_or_wrong_typed_file_raises(self):
        self.write("[worker\n")
        with self.assertRaisesRegex(ValueError, r"config\.toml"):
            config.worker_model()
        self.write("[worker]\nmodel = 3\n")
        with self.assertRaisesRegex(ValueError, r"config\.toml.*string"):
            config.worker_model()

    # dispatch resolves the default at call time
    def test_dispatch_default_follows_config_and_explicit_wins(self):
        con, rt = store.connect(), FakeRuntime()
        self.addCleanup(con.close)

        def go(**kw):
            t = core.dispatch(con, rt, orch_thread="thread-A", repo="demo", title="T", instructions="i",
                              done_when="d", model_reason="r", task_type="code", **kw)
            return store.get_task(con, t["id"])["model"]
        self.assertEqual(go(), "claude-sonnet-5-5")
        self.write('[worker]\nmodel = "sol"\n')
        self.assertEqual(go(), "gpt-6.1-sol")
        self.assertEqual(go(model="sonnet"), "claude-sonnet-5-5")
        self.write('[worker]\nmodel = "opus"\n')
        before = len(store.open_tasks(con))
        with self.assertRaises(ValueError):
            go()
        self.assertEqual(len(store.open_tasks(con)), before)

    # CLI
    def test_get_show_set_roundtrip(self):
        self.assertEqual(self.run_cli("config", "get", "worker-model"), (0, "sonnet\n", ""))
        code, out, _ = self.run_cli("config", "show")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["worker-model"], {"value": "sonnet", "source": "default"})
        self.assertEqual(self.run_cli("config", "set", "worker-model", "sol")[0], 0)
        self.assertEqual(self.run_cli("config", "get", "worker-model"), (0, "sol\n", ""))
        self.assertEqual(json.loads(self.run_cli("config", "show")[1])["worker-model"],
                         {"value": "sol", "source": "file"})
        with mock.patch.dict(os.environ, {"ORCHD_WORKER_MODEL": "sonnet"}):
            shown = json.loads(self.run_cli("config", "show")[1])["worker-model"]
        self.assertEqual(shown, {"value": "sonnet", "source": "env"})

    def test_show_reports_the_file_path(self):
        self.assertEqual(json.loads(self.run_cli("config", "show")[1])["file"], str(self.file))

    def test_set_rejects_invalid_and_writes_nothing(self):
        code, _, err = self.run_cli("config", "set", "worker-model", "opus")
        self.assertEqual(code, 1)
        self.assertIn("sol, sonnet", err)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.run_cli("config", "set", "nope", "x")[0], 1)

    def test_set_keeps_other_settings_and_unset_restores_default(self):
        self.write('[other]\nkeep = "me"\n[worker]\nmodel = "sol"\n')
        self.run_cli("config", "set", "worker-model", "sonnet")
        self.assertIn('keep = "me"', self.file.read_text())
        self.assertEqual(config.worker_model(), ("sonnet", "file"))
        self.assertEqual(self.run_cli("config", "unset", "worker-model")[0], 0)
        self.assertIn('keep = "me"', self.file.read_text())
        self.assertEqual(config.worker_model(), ("sonnet", "default"))

    def test_crlf_file_keeps_every_other_byte(self):
        def put(raw):
            self.dir.mkdir(parents=True, exist_ok=True)
            self.file.write_bytes(raw)
        put(b'# mine\r\n[other]\r\nkeep = "me"\r\n[worker]\r\nmodel = "sol"  # why\r\n[z]\r\nq = 1\r\n')
        self.assertEqual(self.run_cli("config", "set", "worker-model", "sonnet")[0], 0)
        self.assertEqual(self.file.read_bytes(),
                         b'# mine\r\n[other]\r\nkeep = "me"\r\n[worker]\r\nmodel = "sonnet"\r\n[z]\r\nq = 1\r\n')
        self.assertEqual(self.run_cli("config", "unset", "worker-model")[0], 0)
        self.assertEqual(self.file.read_bytes(), b'# mine\r\n[other]\r\nkeep = "me"\r\n[worker]\r\n[z]\r\nq = 1\r\n')
        self.assertEqual(self.run_cli("config", "set", "worker-model", "sol")[0], 0)  # inserted line follows the file
        self.assertEqual(self.file.read_bytes(),
                         b'# mine\r\n[other]\r\nkeep = "me"\r\n[worker]\r\nmodel = "sol"\r\n[z]\r\nq = 1\r\n')
        put(b'[other]\r\nkeep = 1\r\n')  # new table appended in the file's style
        self.assertEqual(self.run_cli("config", "set", "worker-model", "sol")[0], 0)
        self.assertEqual(self.file.read_bytes(), b'[other]\r\nkeep = 1\r\n\r\n[worker]\r\nmodel = "sol"\r\n')

    def test_lf_file_untouched(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes(b'# a\n[worker]\nmodel = "sol"\n')
        self.assertEqual(self.run_cli("config", "set", "worker-model", "sonnet")[0], 0)
        self.assertEqual(self.file.read_bytes(), b'# a\n[worker]\nmodel = "sonnet"\n')

    def test_get_surfaces_invalid_config(self):
        self.write('[worker]\nmodel = "opus"\n')
        code, _, err = self.run_cli("config", "get", "worker-model")
        self.assertEqual(code, 1)
        self.assertIn("opus", err)

    # review blockers: set/unset must not rewrite anything else in the file
    def test_set_preserves_escapes_and_quoted_keys(self):
        import tomllib
        original = '# mine\nnote = "a\\nb"\n["a.b"]\nx = 1\n\n[worker]\nmodel = "sol"  # why\n'
        self.write(original)
        self.assertEqual(self.run_cli("config", "set", "worker-model", "sonnet")[0], 0)
        text = self.file.read_text()
        self.assertIn('note = "a\\nb"', text)
        self.assertIn('["a.b"]', text)
        self.assertIn("# mine", text)
        expected = tomllib.loads(original)
        expected["worker"]["model"] = "sonnet"
        self.assertEqual(tomllib.loads(text), expected)
        self.assertEqual(self.run_cli("config", "unset", "worker-model")[0], 0)
        self.assertIn('note = "a\\nb"', self.file.read_text())
        self.assertEqual(config.worker_model(), ("sonnet", "default"))

    def test_set_refuses_dotted_and_inline_forms_unchanged(self):
        for original in ('worker.model = "sol"\n', 'worker = { model = "sol" }\n'):
            self.write(original)
            code, _, err = self.run_cli("config", "set", "worker-model", "sonnet")
            self.assertEqual(code, 1, original)
            self.assertEqual(self.file.read_text(), original)
            self.assertIn("config.toml", err)
            code, _, err = self.run_cli("config", "unset", "worker-model")
            self.assertEqual(code, 1, original)
            self.assertEqual(self.file.read_text(), original)

    def test_set_uses_unique_temp_file(self):
        self.write('[worker]\nmodel = "sol"\n')
        stale = self.dir / "config.toml.tmp"
        stale.write_text("other writer")
        self.run_cli("config", "set", "worker-model", "sonnet")
        self.assertEqual(stale.read_text(), "other writer")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["config.toml", "config.toml.tmp"])

    def test_permissions(self):
        self.run_cli("config", "set", "worker-model", "sol")
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.file.chmod(0o640)
        self.run_cli("config", "set", "worker-model", "sonnet")
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o640)
        self.run_cli("config", "unset", "worker-model")
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o640)

    # every layer is validated, even when a higher layer would win
    def test_invalid_lower_layers_are_not_masked(self):
        self.write('[worker]\nmodel = "opus"\n')
        with self.assertRaisesRegex(ValueError, r"config\.toml.*opus"):
            config.worker_model("sonnet")
        with mock.patch.dict(os.environ, {"ORCHD_WORKER_MODEL": "sol"}):
            with self.assertRaisesRegex(ValueError, r"config\.toml.*opus"):
                config.worker_model()
        self.file.unlink()
        with mock.patch.dict(os.environ, {"ORCHD_WORKER_MODEL": "opus"}):
            with self.assertRaisesRegex(ValueError, "ORCHD_WORKER_MODEL.*opus"):
                config.worker_model("sol")

    def test_explicit_empty_or_falsy_values_are_errors_only_none_is_omitted(self):
        for bad in ("", 0, False, [], 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.worker_model(bad)
        self.assertEqual(config.worker_model(None), ("sonnet", "default"))

    def test_wrong_structure_fails_closed(self):
        for text in ('worker = "sol"\n', 'worker = ["sol"]\n', '[worker]\nmodel = ["sol"]\n'):
            self.write(text)
            with self.assertRaisesRegex(ValueError, r"config\.toml"):
                config.worker_model()

    def test_bad_current_value_can_still_be_replaced_or_unset(self):
        self.write('[worker]\nmodel = "opus"\n')
        self.assertEqual(self.run_cli("config", "set", "worker-model", "sol")[0], 0)
        self.write('[worker]\nmodel = "opus"\n')
        self.assertEqual(self.run_cli("config", "unset", "worker-model")[0], 0)
        self.assertEqual(config.worker_model(), ("sonnet", "default"))

    def test_directory_at_config_path_is_an_error(self):
        self.file.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, r"config\.toml.*not a file"):
            config.worker_model()

    def test_show_names_the_file_source(self):
        shown = json.loads(self.run_cli("config", "show")[1])
        self.assertEqual(shown["file-source"], "ORCHD_CONFIG_DIR")
        override = Path(self.tmp.name) / "x.toml"
        with mock.patch.dict(os.environ, {"ORCHD_CONFIG_FILE": str(override)}):
            shown = json.loads(self.run_cli("config", "show")[1])
        self.assertEqual((shown["file"], shown["file-source"]), (str(override), "ORCHD_CONFIG_FILE"))

    def test_mcp_dispatch_schema_does_not_hardcode_default(self):
        from orchd import mcp_server
        prop = {t["name"]: t for t in mcp_server.TOOLS}["dispatch"]["inputSchema"]["properties"]["model"]
        self.assertNotIn("default", prop)
        self.assertIn("orchd config", prop["description"])


if __name__ == "__main__":
    unittest.main()
