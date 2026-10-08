"""Explicit Orch actions replace the removed legacy command spellings."""
import contextlib
import io
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import cli
from orchd.runtime import DEFAULT_ORCH_MODEL, ORCH_MODELS


class OrchCommandsTest(unittest.TestCase):
    def commands(self):
        for model in (None, *ORCH_MODELS):
            for detach in (False, True):
                flags = ([] if model is None else ["--model", model]) + (["--no-attach"] if detach else [])
                yield ["orch", "start", *flags], "start", dict(model=model or DEFAULT_ORCH_MODEL, no_attach=detach)
        yield ["orch", "stop", "old"], "stop", dict(orch_id="old")
        for old in ([], ["old"]):
            for model in (None, *ORCH_MODELS):
                for dry in (False, True):
                    flags = ([] if model is None else ["--model", model]) + (["--dry-run"] if dry else [])
                    yield ["orch", "restart", *old, *flags], "restart", dict(old_id=old[0] if old else None, model=model, dry_run=dry)
        for flags in ([], ["--all"], ["--json"], ["--all", "--json"], ["--restore", "old"], ["--restore", "old", "--all", "--json"]):
            yield ["orch", "list", *flags], "list", dict(all="--all" in flags, json="--json" in flags, restore="old" if "--restore" in flags else None)
        for flags in ([], ["--viewer"]):
            yield ["orch", "attach", "old", *flags], "attach", dict(orch_id="old", viewer=bool(flags))

    def test_parser_defaults_and_flags(self):
        parser = cli.build_parser()
        for argv, action, fields in self.commands():
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual((args.cmd, args.orch_action), ("orch", action))
                for field, expected in fields.items():
                    self.assertEqual(getattr(args, field), expected)

    def invoke(self, argv, *, failure=None, restart_status="done", registry=True):
        con, runtime, snapshot = Mock(name="connection"), Mock(name="runtime"), Mock(name="snapshot")
        snapshot.con.execute.return_value.fetchone.return_value = (0, "main", "/mock/snapshot.db")
        calls = Mock()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            # Other suite tests may leave SQLite connections for GC. Their
            # ResourceWarnings are unrelated to these mocked CLI outputs.
            stack.enter_context(warnings.catch_warnings())
            warnings.simplefilter("ignore", ResourceWarning)
            connect = stack.enter_context(patch("orchd.cli.store.connect", return_value=con))
            stack.enter_context(patch("orchd.cli.store.home", return_value=Path("/mock")))
            stack.enter_context(patch("pathlib.Path.exists", return_value=registry))
            stack.enter_context(patch("orchd.cli.Runtime", return_value=runtime))
            snap = stack.enter_context(patch("orchd.stats.open_snapshot", return_value=snapshot))
            for name in ("core.start_orch", "core.stop_orch", "orch_restart.restart", "inventory.attach", "inventory.restore", "inventory.list_orchs", "inventory.render"):
                mock = stack.enter_context(patch("orchd." + name))
                calls.attach_mock(mock, name.replace(".", "_"))
                if name == "core.start_orch":
                    mock.return_value = dict(id="new", model="sonnet", job_id="job")
                elif name == "orch_restart.restart":
                    mock.return_value = dict(status=restart_status, new_orch="new", first_prompt="hello")
                elif name == "inventory.list_orchs":
                    mock.return_value = dict(orchs=[], counts={})
                elif name == "inventory.render":
                    mock.return_value = "inventory"
                if failure and name == failure:
                    mock.side_effect = ValueError("mock failure")
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
        # Record meaningful side effects as well as argument forwarding.
        return code, stdout.getvalue(), stderr.getvalue(), calls.mock_calls, connect.call_args_list, runtime.mock_calls, con.mock_calls, snap.call_args_list, snapshot.mock_calls

    def test_canonical_actions_forward_to_existing_implementations(self):
        for argv, action, fields in self.commands():
            with self.subTest(argv=argv):
                code, stdout, stderr, calls, _, runtime_calls, *_ = self.invoke(argv)
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                if action == "start":
                    call = calls[0]
                    self.assertEqual(call[0], "core_start_orch")
                    self.assertEqual(call.args[2:], (fields["model"],))
                    self.assertEqual(bool(runtime_calls), not fields["no_attach"])
                elif action == "stop":
                    self.assertEqual(calls[0][0], "core_stop_orch")
                    self.assertEqual(calls[0].args[2:], ("old",))
                elif action == "restart":
                    self.assertEqual(calls[0][0], "orch_restart_restart")
                    self.assertEqual(calls[0].args[2:], (fields["old_id"], fields["model"], fields["dry_run"]))
                elif action == "attach":
                    self.assertEqual(calls[0][0], "inventory_attach")
                    self.assertEqual(calls[0].args[2:], ("old",))
                    self.assertEqual(calls[0].kwargs, dict(viewer=fields["viewer"]))
                elif fields["restore"]:
                    self.assertEqual(calls[0][0], "inventory_restore")
                    self.assertEqual(calls[0].args[1:], ("old",))
                else:
                    self.assertEqual(calls[0][0], "inventory_list_orchs")
                    self.assertEqual(calls[0].kwargs, dict(observe=True))
                    if fields["json"]:
                        self.assertIn('"orchs"', stdout)
                    else:
                        self.assertEqual(calls[1].kwargs, dict(all=fields["all"]))

    def test_errors_and_nonzero_paths(self):
        for argv, target in (
            (["orch", "restart"], "orch_restart.restart"),
            (["orch", "list"], "inventory.list_orchs"),
            (["orch", "list", "--restore", "old"], "inventory.restore"),
            (["orch", "attach", "old"], "inventory.attach"),
        ):
            with self.subTest(argv=argv):
                result = self.invoke(argv, failure=target)
                self.assertEqual(result[0], 1)
                self.assertIn("mock failure", result[2])
                self.assertNotIn("orch-restart", result[2])
                self.assertNotIn("orchd orchs", result[2])
        for status, code in (("failed", 1), ("dry-run", 0)):
            self.assertEqual(self.invoke(["orch", "restart"], restart_status=status)[0], code)
        for argv in (["orch", "list"], ["orch", "attach", "old"]):
            self.assertEqual(self.invoke(argv, registry=False)[:3], (1, "", "orchd: no registry DB\n"))

    def test_start_and_stop_propagate_existing_errors(self):
        for argv, target in ((["orch", "start"], "core.start_orch"), (["orch", "stop", "old"], "core.stop_orch")):
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, "mock failure"):
                self.invoke(argv, failure=target)

    def test_removed_names_and_implicit_start_explain_migration_without_state_access(self):
        removed = {"orch-stop": "orch stop", "orch-restart": "orch restart", "orchs": "orch list", "attach": "orch attach"}
        with patch("orchd.cli.store.connect") as connect, patch("orchd.cli.Runtime") as runtime:
            for old, new in removed.items():
                for suffix in ([], ["old"], ["--help"]):
                    stderr = io.StringIO()
                    with self.subTest(old=old, suffix=suffix), contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as error:
                            cli.main([old, *suffix])
                        self.assertEqual(error.exception.code, 2)
                        self.assertIn(f"use `orchd {new}` instead", stderr.getvalue())
            for flags in ([], ["--model", "sonnet"], ["--no-attach"], ["--model", "opus", "--no-attach"]):
                stderr = io.StringIO()
                with self.subTest(flags=flags), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as error:
                        cli.main(["orch", *flags])
                    self.assertEqual(error.exception.code, 2)
                    for action in ("start", "stop", "restart", "list", "attach"):
                        self.assertIn(action, stderr.getvalue())
                    self.assertIn("orchd orch start", stderr.getvalue())
            connect.assert_not_called()
            runtime.assert_not_called()

    def test_invalid_arguments_exit_before_state_access(self):
        with patch("orchd.cli.store.connect") as connect, patch("orchd.cli.Runtime") as runtime:
            for argv in (["orch", "stop"], ["orch", "attach"], ["orch", "start", "--model", "invalid"],
                         ["orch", "restart", "--model", "invalid"], ["orch", "list", "--restore"]):
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.main(argv)
                    self.assertEqual(error.exception.code, 2)
            connect.assert_not_called()
            runtime.assert_not_called()

    def test_help_exits_without_state_access(self):
        with patch("orchd.cli.store.connect") as connect, patch("orchd.cli.Runtime") as runtime:
            for action in (None, "start", "stop", "restart", "list", "attach"):
                argv = ["orch"] + ([] if action is None else [action]) + ["--help"]
                with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.main(argv)
                    self.assertEqual(error.exception.code, 0)
            connect.assert_not_called()
            runtime.assert_not_called()
