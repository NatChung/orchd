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
        git("-C", str(self.work), "checkout", "-q", "-b", "task-branch")
        self.script("preflight", 'echo "ns=$ORCHD_TEST_NAMESPACE db=$ORCHD_TEST_DB_NAME redis=$ORCHD_TEST_REDIS_PREFIX"\n'
                                 'echo "leak=${SECRET:-none}"')
        self.script("check-pr", 'echo "input=$ORCHD_PR_INPUT"; test -f "$ORCHD_PR_INPUT"')
        git("-C", str(self.work), "remote", "add", "origin", "https://github.com/acme/product.git")
        self.commit(self.work, "README", "x")
        self.base = git("-C", str(self.work), "rev-parse", "HEAD")
        git("-C", str(self.work), "update-ref", "refs/remotes/origin/main", self.base)
        self.con = store.connect(self.root / "home" / "orchd.db")
        store.create_task(self.con, id="abc12345", repo="product", repo_path=str(self.work), title="T",
                          instructions="i", done_when="d", orch_thread="o", codex_bin="c", base=self.base, base_branch="main",
                          branch=git("-C", str(self.work), "rev-parse", "--abbrev-ref", "HEAD"), worktree=str(self.work))
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

    def script_at_base(self, name, body):
        """Commit the script and move the task's base to it, as if the worktree had been created from that commit."""
        self.script(name, body)
        self.commit(self.work, f"{name}.touch", body)
        self.base = git("-C", str(self.work), "rev-parse", "HEAD")
        self.con.execute("UPDATE tasks SET base=? WHERE id='abc12345'", (self.base,))
        self.task = store.get_task(self.con, "abc12345")

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
        self.script_at_base("preflight", "echo boom >&2; exit 3")
        record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertFalse(record["passed"])
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("boom", record["output"])
        self.publish(dict(self.definition, checks={"preflight": dict(PREFLIGHT, timeout_seconds=1)}))
        self.script_at_base("preflight", "sleep 5")
        record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertFalse(record["passed"])
        self.assertIsNone(record["exit_code"])
        self.assertIn("timed out", record["output"])

    def test_output_is_bounded(self):
        self.script_at_base("preflight", "yes x | head -c 200000")
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


class BaseAndHeadTest(ChecksBase):
    def gate(self, args):
        return delivery_checks.gate_pr(self.con, store.get_task(self.con, "abc12345"), args, self.root)

    def test_preflight_runs_the_base_not_a_script_committed_after_it(self):
        self.script_at_base("preflight", "echo base-version; exit 1")
        self.script("preflight", "echo tampered; exit 0")
        self.commit(self.work, "later", "x")
        record = delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertFalse(record["passed"])
        self.assertIn("base-version", record["output"])
        self.assertEqual((record["base_sha"], record["head_sha"]), (self.base, self.base))
        with self.assertRaises(delivery_checks.CheckError):
            self.gate(["pr", "create"])

    def test_preflight_ignores_uncommitted_changes(self):
        self.script_at_base("preflight", "exit 1")
        self.script("preflight", "exit 0")
        self.assertFalse(delivery_checks.run(self.con, self.task, "preflight", self.root)["passed"])

    def test_preflight_leaves_no_worktree_behind(self):
        delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.assertEqual(git("-C", str(self.work), "worktree", "list").count("\n"), 0)

    def test_gate_refuses_a_preflight_that_did_not_run_the_base(self):
        delivery_checks.run(self.con, self.task, "preflight", self.root)
        self.commit(self.work, "later", "x")
        self.con.execute("UPDATE check_runs SET head_sha=?", (git("-C", str(self.work), "rev-parse", "HEAD"),))
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate(["pr", "create"])
        self.assertIn("base", str(raised.exception))

    def prepare(self):
        self.commit(self.work, "f", "1")
        delivery_checks.run(self.con, store.get_task(self.con, "abc12345"), "preflight", self.root)

    def test_head_and_repo_cannot_be_chosen_by_the_caller(self):
        self.prepare()
        branch = self.task["branch"]
        cases = [["--head", f"attacker:{branch}"], ["--head", "attacker:main"], ["--head", branch],
                 ["--head", f"acme:{branch}"], ["--head=other"], ["-H", "other"], ["--repo", "evil/product"],
                 ["--repo", "acme/product"], ["-R", "acme/product"], ["--repo=acme/product"]]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(delivery_checks.CheckError) as raised:
                self.gate(["pr", "create", *extra])
            self.assertIn("allowed", str(raised.exception))
        self.assertEqual([r["kind"] for r in delivery_checks.records(self.con, "abc12345")], ["preflight"])

    def test_only_whitelisted_arguments_are_accepted(self):
        self.prepare()
        for extra in (["--fill"], ["-t", "T"], ["--web"], ["--unknown", "x"], ["positional"], ["--"],
                      ["--no-maintainer-edit"], ["--template", "x"], ["-d"]):
            with self.subTest(extra=extra), self.assertRaises(delivery_checks.CheckError) as raised:
                self.gate(["pr", "create", *extra])
            self.assertIn("--title, --body, --body-file", str(raised.exception))

    def test_gh_args_are_rebuilt_with_the_task_branch_and_origin_repo(self):
        self.prepare()
        out = self.gate(["pr", "create", "--title", "T", "--body=B", "--base", "main", "--draft", "--label", "a",
                         "--reviewer", "r", "--assignee=me"])
        self.assertEqual(out[:2], ["pr", "create"])
        self.assertEqual(out[out.index("--head") + 1], "task-branch")
        self.assertEqual(out[out.index("--repo") + 1], "acme/product")
        self.assertEqual(out.count("--head"), 1)
        self.assertEqual(out.count("--repo"), 1)
        for flag in ("--title", "--body", "--base", "--draft", "--label", "--reviewer", "--assignee"):
            self.assertIn(flag, out)

    def test_body_file_content_is_sent_as_body(self):
        self.prepare()
        body = self.root / "b.md"
        body.write_text("exact")
        out = self.gate(["pr", "create", "--body-file", str(body)])
        self.assertNotIn("--body-file", out)
        self.assertEqual(out[out.index("--body") + 1], "exact")

    def test_base_must_be_the_task_base_branch(self):
        self.prepare()
        git("-C", str(self.work), "update-ref", "refs/remotes/origin/other", self.base)
        for extra in (["--base", "other"], ["--base", "nonexistent"], ["--base=other"], ["--base", "origin/main"]):
            with self.subTest(extra=extra), self.assertRaises(delivery_checks.CheckError) as raised:
                self.gate(["pr", "create", *extra])
            self.assertIn("--base", str(raised.exception))
            self.assertIn("main", str(raised.exception))
        self.assertIsNotNone(self.gate(["pr", "create", "--base", "main"]))

    def test_repeated_single_value_arguments_are_refused(self):
        self.prepare()
        good = self.root / "ok.md"
        good.write_text("ok")
        cases = [["--body-file", "-", "--body-file", str(good)], ["--body-file", str(good), "--body-file", "-"],
                 ["--title", "a", "--title", "b"], ["--title=a", "--title", "b"], ["--body", "a", "--body=b"],
                 ["--base", "main", "--base", "main"], ["--draft", "--draft"]]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(delivery_checks.CheckError) as raised:
                self.gate(["pr", "create", *extra])
            self.assertIn("more than once", str(raised.exception))

    def test_body_and_body_file_are_mutually_exclusive(self):
        self.prepare()
        body = self.root / "b.md"
        body.write_text("x")
        for extra in (["--body", "a", "--body-file", str(body)], ["--body-file", str(body), "--body=a"]):
            with self.subTest(extra=extra), self.assertRaises(delivery_checks.CheckError) as raised:
                self.gate(["pr", "create", *extra])
            self.assertIn("mutually exclusive", str(raised.exception))

    def test_label_reviewer_assignee_may_repeat(self):
        self.prepare()
        out = self.gate(["pr", "create", "--label", "a", "--label", "b", "--reviewer", "x", "--reviewer", "y",
                         "--assignee", "m", "--assignee", "n"])
        self.assertEqual(out.count("--label"), 2)
        self.assertEqual(out.count("--reviewer"), 2)
        self.assertEqual(out.count("--assignee"), 2)

    def test_gh_always_gets_the_task_base_branch(self):
        self.prepare()
        for given in ([], ["--base", "main"]):
            out = self.gate(["pr", "create", *given])
            self.assertEqual(out.count("--base"), 1)
            self.assertEqual(out[out.index("--base") + 1], "main")

    def test_task_without_a_recorded_base_branch_is_refused(self):
        self.prepare()
        self.con.execute("UPDATE tasks SET base_branch=NULL")
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate(["pr", "create"])
        self.assertIn("base branch", str(raised.exception))

    def test_base_branch_that_moved_ahead_is_allowed_but_a_diverged_one_is_refused(self):
        self.prepare()
        self.commit(self.work, "g", "2")
        ahead = git("-C", str(self.work), "rev-parse", "HEAD")
        git("-C", str(self.work), "reset", "-q", "--hard", "HEAD~1")
        git("-C", str(self.work), "update-ref", "refs/remotes/origin/main", ahead)
        self.assertIsNotNone(self.gate(["pr", "create"]))
        other = git("-C", str(self.work), "commit-tree", "-m", "x", git("-C", str(self.work), "rev-parse", "HEAD^{tree}"))
        git("-C", str(self.work), "update-ref", "refs/remotes/origin/main", other)
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate(["pr", "create"])
        self.assertIn("does not contain", str(raised.exception))

    def test_dispatch_records_the_base_branch(self):
        from orchd.runtime import Runtime
        git("init", "-q", "--bare", str(self.root / "o.git"))
        git("clone", "-q", str(self.root / "o.git"), str(self.root / "clone"))
        git("-C", str(self.root / "clone"), "checkout", "-q", "-b", "trunk")
        self.commit(self.root / "clone", "a", "1")
        git("-C", str(self.root / "clone"), "push", "-q", "origin", "trunk")
        git("--git-dir", str(self.root / "o.git"), "symbolic-ref", "HEAD", "refs/heads/trunk")
        git("-C", str(self.root / "clone"), "remote", "set-head", "origin", "-a")
        self.assertEqual(Runtime().base_branch(self.root / "clone"), "trunk")
        self.assertIsNone(Runtime().base_branch(self.work))

    def test_unparseable_origin_is_refused(self):
        self.prepare()
        git("-C", str(self.work), "remote", "set-url", "origin", str(self.root / "somewhere"))
        with self.assertRaises(delivery_checks.CheckError):
            self.gate(["pr", "create"])

    def test_pushed_branch_that_differs_from_the_checked_head_is_refused(self):
        self.prepare()
        branch = self.task["branch"]
        old = git("-C", str(self.work), "rev-parse", "HEAD~1")
        git("-C", str(self.work), "update-ref", f"refs/remotes/origin/{branch}", old)
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate(["pr", "create"])
        self.assertIn("push", str(raised.exception))
        git("-C", str(self.work), "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        self.assertIsNotNone(self.gate(["pr", "create"]))

    def test_pr_check_refuses_a_dirty_worktree_directly(self):
        self.prepare()
        (self.work / "dirty").write_text("x")
        with self.assertRaises(delivery_checks.CheckError):
            delivery_checks.run(self.con, store.get_task(self.con, "abc12345"), "pr", self.root)


class PrGateTest(ChecksBase):
    ARGS = ["pr", "create", "--title", "T", "--body", "Body", "--base", "main", "--label", "a,b", "--label", "c"]

    def gate(self, args=None):
        return delivery_checks.gate_pr(self.con, store.get_task(self.con, "abc12345"), args or self.ARGS, self.root)

    def preflight(self):
        return delivery_checks.run(self.con, store.get_task(self.con, "abc12345"), "preflight", self.root)

    def test_unconfigured_repo_is_not_gated(self):
        (self.root / "config.toml").write_text("")
        self.assertIsNone(self.gate())
        self.assertIsNone(self.gate(["pr", "create", "--head", "anything"]))
        self.assertEqual(delivery_checks.records(self.con, "abc12345"), [])

    def test_missing_preflight_is_refused_with_the_command_to_run(self):
        with self.assertRaises(delivery_checks.CheckError) as raised:
            self.gate()
        self.assertIn("preflight", str(raised.exception))
        self.assertIn("orchd check preflight abc12345", str(raised.exception))

    def test_failed_preflight_is_refused(self):
        self.script_at_base("preflight", "exit 1")
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
        self.assertIsNotNone(self.gate())

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
        self.assertIsNotNone(self.gate())
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
        self.assertIsNotNone(self.gate())  # no checks.pr defined: nothing to run

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
        parsed = delivery_checks.parse_pr_args(["pr", "create", "--title=X", "--base", "dev", "--draft", "--label=z",
                                                "--label", "a,b", "--reviewer", "r"])
        self.assertEqual((parsed["title"], parsed["base"]), ("X", "dev"))
        self.assertEqual(parsed["labels"], ["z", "a", "b"])
        self.assertTrue(parsed["draft"])
        self.assertEqual(parsed["reviewers"], ["r"])


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
        self.orchd.write_text(f'#!/bin/sh\necho "orchd $@" >> {self.calls}\necho "refused: $ORCHD_FAKE" >&2\necho \'["pr","create","--head","b","--repo","o/r"]\'\nexit ${{ORCHD_FAKE_EXIT:-0}}\n')
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
                         ["orchd check gate --worktree /w -- pr create --title T", "gh pr create --head b --repo o/r"])

    def test_refusal_stops_gh_and_shows_the_reason(self):
        result = self.run_gh("pr", "create", ORCHD_WORKTREE="/w", ORCHD_BIN=str(self.orchd),
                             ORCHD_FAKE="no preflight", ORCHD_FAKE_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refused: no preflight", result.stderr)
        self.assertNotIn("gh pr create", self.called())

    def test_gh_gets_only_the_rewritten_arguments(self):
        self.run_gh("pr", "create", "--head", "attacker:main", ORCHD_WORKTREE="/w", ORCHD_BIN=str(self.orchd))
        self.assertNotIn("attacker", self.called().splitlines()[-1])

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


class CliAndMcpTest(ChecksBase):
    def cli(self, *argv):
        import contextlib
        import io
        from orchd import cli
        out, err = io.StringIO(), io.StringIO()
        real = store.connect
        with patch.object(store, "connect", lambda *a: real(self.root / "home" / "orchd.db")), \
                patch("orchd.cli._home_policy", return_value="skip"), \
                patch.dict(os.environ, {"ORCHD_PROJECTS": str(self.root)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_preflight_records_and_show_lists(self):
        code, out, _ = self.cli("check", "preflight", "abc12345")
        self.assertEqual(code, 0)
        self.assertIn("preflight passed", out)
        self.assertIn("orchd_abc12345", out)
        code, out, _ = self.cli("check", "show", "abc12345")
        rows = json.loads(out)
        self.assertEqual([(r["kind"], r["passed"]) for r in rows], [("preflight", 1)])

    def test_failed_preflight_exits_nonzero_with_output(self):
        self.script_at_base("preflight", "echo nope; exit 2")
        code, _, err = self.cli("check", "preflight", "abc12345")
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_unknown_task_and_unconfigured_repo_are_errors(self):
        self.assertEqual(self.cli("check", "preflight", "nosuch")[0], 1)
        (self.root / "config.toml").write_text("")
        code, _, err = self.cli("check", "preflight", "abc12345")
        self.assertEqual(code, 1)
        self.assertIn("no delivery pipeline", err)

    def test_gate_finds_task_by_worktree_and_refuses_without_preflight(self):
        code, _, err = self.cli("check", "gate", "--worktree", str(self.work), "--", "pr", "create", "--title", "T")
        self.assertEqual(code, 1)
        self.assertIn("orchd check preflight abc12345", err)
        self.cli("check", "preflight", "abc12345")
        self.assertEqual(self.cli("check", "gate", "--worktree", str(self.work), "--", "pr", "create")[0], 0)

    def test_gate_refuses_unknown_worktree(self):
        code, _, err = self.cli("check", "gate", "--worktree", str(self.root / "elsewhere"), "--", "pr", "create")
        self.assertEqual(code, 1)
        self.assertIn("no open task", err)

    def test_mcp_check_runs_is_read_only_and_scoped_to_the_task(self):
        from orchd import mcp_server
        delivery_checks.run(self.con, self.task, "preflight", self.root)
        rows = mcp_server.call("check_runs", {"task_id": "abc12345"}, "o", self.con, None)
        self.assertEqual([r["kind"] for r in rows], ["preflight"])
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "check_runs")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        with self.assertRaises(KeyError):
            mcp_server.call("check_runs", {"task_id": "nosuch"}, "o", self.con, None)

    def test_worker_brief_mentions_the_checks(self):
        from orchd import core
        for kind in ("claude", "codex"):
            self.assertIn("orchd check preflight <task-id>", core.worker_brief("orchd", kind))


if __name__ == "__main__":
    unittest.main()
