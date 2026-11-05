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


if __name__ == "__main__":
    unittest.main()
