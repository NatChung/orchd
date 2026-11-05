import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import config, delivery_checks, store, worker_flow

PREFLIGHT = {"argv": ["./scripts/preflight"], "cwd": ".", "timeout_seconds": 20, "allowed_exit_codes": [0],
             "env_allowlist": ["PATH"], "required": True}
PR = dict(PREFLIGHT, argv=["./scripts/check-pr"])


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


class ChecksBase(unittest.TestCase):
    definition = {"schema_version": 1, "id": "delivery", "version": "1.0.0", "stages": [],
                  "checks": {"preflight": PREFLIGHT, "pr": PR}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"ORCHD_CONFIG_FILE": str(self.root / "config.toml"),
                                      "ORCHD_HOME": str(self.root / "home"), "PATH": os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        worker_flow._fetches.clear()
        (self.root / "config.toml").write_text('[delivery_flows]\nproduct = "hub"\n')
        git("init", "-q", "--bare", str(self.root / "hub.git"))
        git("clone", "-q", str(self.root / "hub.git"), str(self.root / "hub"))
        self.publish(self.definition)
        # product worktree
        self.work = self.root / "product"
        git("init", "-q", str(self.work))
        self.script("preflight", 'echo "ns=$ORCHD_TEST_NAMESPACE db=$ORCHD_TEST_DB_NAME redis=$ORCHD_TEST_REDIS_PREFIX"\n'
                                 'echo "leak=${SECRET:-none}"')
        self.script("check-pr", 'echo "input=$ORCHD_PR_INPUT"; test -f "$ORCHD_PR_INPUT"')
        self.commit(self.work, "README", "x")
        self.base = git("-C", str(self.work), "rev-parse", "HEAD")
        self.con = store.connect(self.root / "home" / "orchd.db")
        store.create_task(self.con, id="abc12345", repo="product", repo_path=str(self.work), title="T",
                          instructions="i", done_when="d", orch_thread="o", codex_bin="c", base=self.base,
                          branch="b", worktree=str(self.work))
        self.addCleanup(self.con.close)
        self.task = store.get_task(self.con, "abc12345")

    def publish(self, definition, raw=None):
        hub = self.root / "hub"
        target = hub / "docs/development/delivery.pipeline.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw if raw is not None else json.dumps(definition))
        git("-C", str(hub), "add", ".")
        git("-C", str(hub), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "def")
        git("-C", str(hub), "push", "-q", "origin", "HEAD")
        git("-C", str(hub), "remote", "set-head", "origin", "-a")
        worker_flow._fetches.clear()

    def script(self, name, body):
        file = self.work / "scripts" / name
        file.parent.mkdir(exist_ok=True)
        file.write_text("#!/bin/sh\n" + body + "\n")
        file.chmod(file.stat().st_mode | stat.S_IXUSR)

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        git("-C", str(repo), "add", ".")
        git("-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", name)


class PreflightTest(ChecksBase):
    def test_unconfigured_repo_has_no_definition(self):
        (self.root / "config.toml").write_text("")
        self.assertIsNone(delivery_checks.load("product", self.root))

    def test_hub_without_pipeline_file_is_unconfigured(self):
        git("-C", str(self.root / "hub"), "rm", "-q", "docs/development/delivery.pipeline.json")
        git("-C", str(self.root / "hub"), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "rm")
        git("-C", str(self.root / "hub"), "push", "-q", "origin", "HEAD")
        worker_flow._fetches.clear()
        self.assertIsNone(delivery_checks.load("product", self.root))

    def test_load_pins_commit(self):
        loaded = delivery_checks.load("product", self.root)
        self.assertEqual(loaded["hub_commit"], git("-C", str(self.root / "hub"), "rev-parse", "HEAD"))
        self.assertEqual(loaded["checks"]["preflight"]["argv"], ["./scripts/preflight"])

    def test_invalid_definitions_fail_closed(self):
        bad = [dict(self.definition, schema_version=2),
               dict(self.definition, checks={"preflight": dict(PREFLIGHT, extra=1)}),
               dict(self.definition, checks={"preflight": dict(PREFLIGHT, argv="./x")}),
               dict(self.definition, checks={"preflight": dict(PREFLIGHT, cwd="../out")}),
               dict(self.definition, checks={"preflight": dict(PREFLIGHT, cwd="/abs")}),
               dict(self.definition, checks={"other": PREFLIGHT}),
               dict(self.definition, checks={"preflight": dict(PREFLIGHT, timeout_seconds=0)})]
        for definition in bad:
            with self.subTest(definition=definition["checks"]):
                self.publish(definition)
                with self.assertRaises(delivery_checks.CheckError):
                    delivery_checks.load("product", self.root)
        self.publish(None, raw="{not json")
        with self.assertRaises(delivery_checks.CheckError):
            delivery_checks.load("product", self.root)

    def test_fetch_failure_fails_closed(self):
        git("-C", str(self.root / "hub"), "remote", "set-url", "origin", str(self.root / "gone.git"))
        with self.assertRaises(delivery_checks.CheckError):
            delivery_checks.load("product", self.root)

    def test_preflight_runs_with_task_namespace_and_allowlisted_env(self):
        with patch.dict(os.environ, {"SECRET": "s3"}):
            record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertTrue(record["passed"])
        self.assertEqual(record["base_sha"], self.base)
        self.assertIn("ns=orchd_abc12345", record["output"])
        self.assertIn("db=orchd_test_abc12345", record["output"])
        self.assertIn("redis=orchd:abc12345:", record["output"])
        self.assertIn("leak=none", record["output"])

    def test_namespaces_differ_per_task(self):
        self.assertNotEqual(delivery_checks.namespace("abc12345"), delivery_checks.namespace("def67890"))

    def test_run_is_recorded(self):
        delivery_checks.run(self.con, self.task, "preflight", self.root)
        rows = delivery_checks.records(self.con, "abc12345")
        self.assertEqual([(r["kind"], r["passed"]) for r in rows], [("preflight", 1)])
        self.assertEqual(rows[0]["hub_commit"], delivery_checks.load("product", self.root)["hub_commit"])
        self.assertEqual(json.loads(rows[0]["argv"]), ["./scripts/preflight"])

    def test_failing_and_timed_out_checks_are_recorded_not_passed(self):
        self.script("preflight", "echo boom >&2; exit 3")
        record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertFalse(record["passed"])
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("boom", record["output"])
        self.publish(dict(self.definition, checks={"preflight": dict(PREFLIGHT, timeout_seconds=1)}))
        self.script("preflight", "sleep 5")
        record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertFalse(record["passed"])
        self.assertIsNone(record["exit_code"])
        self.assertIn("timed out", record["output"])

    def test_output_is_bounded(self):
        self.script("preflight", "yes x | head -c 200000")
        record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertLessEqual(len(record["output"]), delivery_checks.OUTPUT_LIMIT + 200)

    def test_run_without_definition_is_an_error(self):
        (self.root / "config.toml").write_text("")
        with self.assertRaises(delivery_checks.CheckError):
            delivery_checks.run(self.con, self.task, "preflight", self.root)

    def test_missing_command_is_a_failed_record(self):
        self.publish(dict(self.definition, checks={"preflight": dict(PREFLIGHT, argv=["./nope"])}))
        record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertFalse(record["passed"])


class PrGateTest(ChecksBase):
    ARGS = ["pr", "create", "--title", "T", "--body", "Body", "--base", "main", "--label", "a,b", "-l", "c"]

    def gate(self, args=None):
        return delivery_checks.gate_pr(self.con, store.get_task(self.con, "abc12345"), args or self.ARGS, self.root)

    def preflight(self):
        return delivery_checks.run(self.con, store.get_task(self.con, "abc12345"), "preflight", self.root)

    def test_unconfigured_repo_is_not_gated(self):
        (self.root / "config.toml").write_text("")
        self.assertIsNone(self.gate())
        self.assertEqual(delivery_checks.records(self.con, "abc12345"), [])

    def test_missing_preflight_is_refused_with_the_command_to_run(self):
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate()
        self.assertIn("preflight", str(raised.exception))
        self.assertIn("orchd check preflight abc12345", str(raised.exception))

    def test_failed_preflight_is_refused(self):
        self.script("preflight", "exit 1")
        self.commit(self.work, "f", "1")
        self.preflight()
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate()
        self.assertIn("did not pass", str(raised.exception))

    def test_preflight_from_an_older_definition_is_refused(self):
        self.preflight()
        self.publish(dict(self.definition, version="1.0.1"))
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate()
        self.assertIn("definition changed", str(raised.exception))

    def test_preflight_from_another_base_is_refused(self):
        self.preflight()
        self.con.execute("UPDATE check_runs SET base_sha='deadbeef'")
        with self.assertRaises(delivery_checks.CheckError):
            self.gate()

    def test_optional_preflight_is_not_required(self):
        self.publish(dict(self.definition, checks={"preflight": dict(PREFLIGHT, required=False), "pr": PR}))
        self.assertIsNone(self.gate())

    def test_dirty_worktree_is_refused(self):
        self.preflight()
        (self.work / "dirty").write_text("x")
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate()
        self.assertIn("uncommitted", str(raised.exception))

    def test_pr_check_receives_exact_inputs_and_is_recorded(self):
        self.script("check-pr", f'cp "$ORCHD_PR_INPUT" {self.root}/pr-input.json')
        self.commit(self.work, "f", "1")
        self.preflight()
        self.assertIsNone(self.gate())
        sent = json.loads((self.root / "pr-input.json").read_text())
        self.assertEqual(sent["title"], "T")
        self.assertEqual(sent["body"], "Body")
        self.assertEqual(sent["base"], "main")
        self.assertEqual(sent["labels"], ["a", "b", "c"])
        self.assertEqual(sent["head_sha"], git("-C", str(self.work), "rev-parse", "HEAD"))
        self.assertEqual(sent["base_sha"], self.base)
        rows = delivery_checks.records(self.con, "abc12345")
        self.assertEqual([r["kind"] for r in rows], ["preflight", "pr"])
        self.assertEqual(rows[1]["head_sha"], sent["head_sha"])

    def test_failed_pr_check_is_refused_with_its_output(self):
        self.script("check-pr", "echo 'missing section: TDD red/green'; exit 1")
        self.commit(self.work, "f", "1")
        self.preflight()
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate()
        self.assertIn("missing section: TDD red/green", str(raised.exception))
        self.assertEqual(delivery_checks.records(self.con, "abc12345")[-1]["passed"], 0)

    def test_definition_without_pr_check_runs_none(self):
        self.publish(dict(self.definition, checks={"preflight": PREFLIGHT}))
        self.preflight()
        self.assertIsNone(self.gate())  # no checks.pr defined: nothing to run

    def test_body_file_is_read_and_stdin_is_refused(self):
        body = self.root / "body.md"
        body.write_text("from file")
        self.script("check-pr", f'cp "$ORCHD_PR_INPUT" {self.root}/pr-input.json')
        self.commit(self.work, "f", "1")
        self.preflight()
        self.gate(["pr", "create", "--body-file", str(body)])
        self.assertEqual(json.loads((self.root / "pr-input.json").read_text())["body"], "from file")
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate(["pr", "create", "--body-file", "-"])
        self.assertIn("stdin", str(raised.exception))

    def test_parse_pr_args_forms(self):
        parsed = delivery_checks.parse_pr_args(["pr", "create", "--title=X", "-B", "dev", "--head", "me:br",
                                                "--draft", "--fill", "-R", "o/r", "--label=z"])
        self.assertEqual((parsed["title"], parsed["base"], parsed["head"], parsed["repo"]), ("X", "dev", "me:br", "o/r"))
        self.assertEqual(parsed["labels"], ["z"])
        self.assertTrue(parsed["draft"] and parsed["fill"])


class WrapperGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.calls = root / "calls"
        self.bin = root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\necho "gh $@" >> {self.calls}\n')
        gh.chmod(0o755)
        self.orchd = root / "orchd"
        self.orchd.write_text(f'#!/bin/sh\necho "orchd $@" >> {self.calls}\necho "refused: $ORCHD_FAKE" >&2\nexit ${{ORCHD_FAKE_EXIT:-0}}\n')
        self.orchd.chmod(0o755)
        self.wrapper = Path(__file__).resolve().parents[1] / "orchd" / "worker_bin" / "gh"
        self.env = {"PATH": f"{self.bin}:{os.defpath}", "ORCHD_CONFIG_DIR": str(root / "cfg"), "HOME": str(root)}

    def run_gh(self, *args, **extra):
        return subprocess.run([str(self.wrapper), *args], env=dict(self.env, **extra), capture_output=True, text=True)

    def called(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def test_pr_create_goes_through_orchd_gate_then_gh(self):
        result = self.run_gh("pr", "create", "--title", "T", ORCHD_WORKTREE="/w", ORCHD_BIN=str(self.orchd))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called().splitlines(),
                         ["orchd check gate --worktree /w -- pr create --title T", "gh pr create --title T"])

    def test_refusal_stops_gh_and_shows_the_reason(self):
        result = self.run_gh("pr", "create", ORCHD_WORKTREE="/w", ORCHD_BIN=str(self.orchd),
                             ORCHD_FAKE="no preflight", ORCHD_FAKE_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refused: no preflight", result.stderr)
        self.assertNotIn("gh pr create", self.called())

    def test_other_commands_and_non_workers_are_not_gated(self):
        self.run_gh("pr", "view", ORCHD_WORKTREE="/w", ORCHD_BIN=str(self.orchd))
        self.run_gh("pr", "create")
        self.assertEqual(self.called().splitlines(), ["gh pr view", "gh pr create"])

    def test_worker_without_orchd_binary_fails_closed(self):
        result = self.run_gh("pr", "create", ORCHD_WORKTREE="/w", ORCHD_BIN="/nonexistent/orchd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot run", result.stderr)
        self.assertNotIn("gh pr create", self.called())

    def test_worker_env_names_the_orchd_binary(self):
        from orchd.runtime import worker_env
        with patch.dict(os.environ, {"ORCHD_HOME": self.temp.name}):
            env = worker_env("/w")
        self.assertTrue(env["ORCHD_BIN"].endswith("orchd"))


if __name__ == "__main__":
    unittest.main()
