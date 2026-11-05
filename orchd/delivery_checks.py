"""Hub-defined delivery checks run by orchd (#119): a preflight and a check before `gh pr create`.

The definition is `docs/development/delivery.pipeline.json` at the Hub's fetched origin/HEAD commit (see
docs/design/staged-pipeline.md). Only `repos` and `checks` are read here; stages belong to the staged pipeline (#121)."""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from . import config, paths, store, worker_flow
from .runtime import redact

DEFINITION = "docs/development/delivery.pipeline.json"
KINDS = ("preflight", "pr")
OUTPUT_LIMIT = 20000
CHECK_KEYS = {"argv", "cwd", "timeout_seconds", "allowed_exit_codes", "env_allowlist", "required"}


class CheckError(Exception):
    """A check could not be loaded or run; the message says why and what to do."""


def namespace(task_id):
    """Per-task names for tests that touch shared services, so parallel workers do not share a DB or Redis keys."""
    safe = re.sub(r"[^A-Za-z0-9]", "_", task_id)
    return {"ORCHD_TEST_NAMESPACE": f"orchd_{safe}", "ORCHD_TEST_DB_NAME": f"orchd_test_{safe}",
            "ORCHD_TEST_REDIS_PREFIX": f"orchd:{task_id}:"}


def _validate(definition):
    if not isinstance(definition, dict) or definition.get("schema_version") != 1:
        raise CheckError("pipeline definition: unsupported schema_version (expected 1)")
    repos = definition.get("repos")
    if repos is None:
        raise CheckError("pipeline definition: repos is required (a list of orchd repo names the checks apply to); "
                         "add it to enable the pipeline per repo")
    if (not isinstance(repos, list) or not all(isinstance(r, str) and r for r in repos)
            or len(set(repos)) != len(repos)):
        raise CheckError("pipeline definition: repos must be an array of unique, non-empty orchd repo names")
    checks = definition.get("checks")
    if not isinstance(checks, dict):
        raise CheckError("pipeline definition: checks must be an object")
    for kind, check in checks.items():
        where = f"pipeline definition: checks.{kind}"
        if kind not in KINDS:
            raise CheckError(f"{where}: unsupported check type (supported: {', '.join(KINDS)})")
        if not isinstance(check, dict) or set(check) != CHECK_KEYS:
            raise CheckError(f"{where}: must have exactly {sorted(CHECK_KEYS)}")
        argv, cwd = check["argv"], check["cwd"]
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise CheckError(f"{where}.argv must be a non-empty array of strings")
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            raise CheckError(f"{where}.cwd must be a relative path inside the worktree")
        timeout = check["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= 3600:
            raise CheckError(f"{where}.timeout_seconds must be an integer from 1 to 3600")
        codes = check["allowed_exit_codes"]
        if (not isinstance(codes, list) or not codes
                or not all(isinstance(c, int) and not isinstance(c, bool) for c in codes)):
            raise CheckError(f"{where}.allowed_exit_codes must be a non-empty array of integers")
        names = check["env_allowlist"]
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise CheckError(f"{where}.env_allowlist must be an array of names")
        if not isinstance(check["required"], bool):
            raise CheckError(f"{where}.required must be a boolean")


def load(repo, projects=None):
    """The pinned checks for a product repo, or None when it has no pipeline (existing behavior is kept).
    Configured but unreadable or invalid raises CheckError: the checks fail closed."""
    projects = Path(projects) if projects is not None else paths.projects_dir()
    try:
        hub = config.delivery_flows().get(repo)
    except ValueError as error:
        raise CheckError(f"delivery flow configuration: {error}") from error
    if hub is None:
        return None
    source = projects / hub
    deadline = time.monotonic() + worker_flow.BRIEF_BUDGET
    try:
        worker_flow._fetch(source, deadline)
        commit = worker_flow._git(source, deadline, "rev-parse", "refs/remotes/origin/HEAD^{commit}")
    except (OSError, subprocess.SubprocessError) as error:
        raise CheckError(f"Hub {hub} could not be fetched; checks fail closed ({worker_flow._error(error)})") from error
    try:
        worker_flow._git(source, deadline, "cat-file", "-e", f"{commit}:{DEFINITION}")
    except subprocess.CalledProcessError:
        return None  # the Hub defines no pipeline at this commit
    except (OSError, subprocess.SubprocessError) as error:
        raise CheckError(f"Hub {hub}: {worker_flow._error(error)}") from error
    try:
        blob = subprocess.run(["git", "-C", str(source), "show", f"{commit}:{DEFINITION}"], capture_output=True,
                              check=True, timeout=worker_flow.FETCH_TIMEOUT).stdout
        definition = json.loads(blob)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise CheckError(f"Hub {hub}: {DEFINITION} at {commit} is unreadable ({type(error).__name__})") from error
    _validate(definition)
    if repo not in definition["repos"]:
        return None  # the Hub defines a pipeline, but not for this repo: same as no definition
    return {"hub": hub, "hub_commit": commit, "definition_sha256": sha256(blob).hexdigest(),
            "checks": definition["checks"]}


def _git(worktree, *args):
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, check=True).stdout.strip()


def _bounded(text):
    text = redact(text)
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + f"\n[output truncated at {OUTPUT_LIMIT} characters]"


def _dirty(worktree):
    return _git(worktree, "status", "--porcelain", "--untracked-files=all")


def run(con, task, kind, projects=None, env=None):
    """Run one pinned check without a shell and record it. Returns the stored row.

    preflight runs on a clean detached checkout of the task's base commit; pr runs in the task worktree, which must
    be clean, so it checks exactly HEAD. The SHA actually run is recorded as head_sha."""
    if kind not in KINDS:
        raise CheckError(f"unknown check {kind!r}; expected one of {', '.join(KINDS)}")
    loaded = load(task["repo"], projects)
    if loaded is None:
        raise CheckError(f"no delivery pipeline is configured for repo {task['repo']}")
    check = loaded["checks"].get(kind)
    if check is None:
        raise CheckError(f"the pinned definition ({loaded['hub']}@{loaded['hub_commit'][:12]}) has no checks.{kind}")
    worktree = Path(task["worktree"] or "")
    if not worktree.is_dir():
        raise CheckError(f"task {task['id']} has no worktree to run the check in")
    scratch = None
    try:
        if kind == "preflight":
            if not task["base"]:
                raise CheckError(f"task {task['id']} has no recorded base commit to run preflight on")
            scratch = Path(tempfile.mkdtemp(prefix=f"preflight-{task['id']}-", dir=_scratch_dir()))
            _git(worktree, "worktree", "add", "--detach", "--force", str(scratch / "tree"), task["base"])
            root = scratch / "tree"
        else:
            if _dirty(worktree):
                raise CheckError("the worktree has uncommitted changes; commit them so the check sees the exact head")
            root = worktree
        head = _git(root, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as error:
        _cleanup(worktree, scratch)
        raise CheckError(f"cannot prepare the checkout: {worker_flow._error(error)}") from error
    except CheckError:
        _cleanup(worktree, scratch)
        raise
    try:
        cwd = (root / check["cwd"]).resolve()
        if root.resolve() not in (cwd, *cwd.parents):
            raise CheckError(f"checks.{kind}.cwd leaves the worktree")
        space = namespace(task["id"])
        child = {name: os.environ[name] for name in check["env_allowlist"] if name in os.environ}
        child.update(space, ORCHD_TASK_ID=task["id"], ORCHD_CHECK_KIND=kind, ORCHD_BASE_SHA=task["base"] or "",
                     ORCHD_HEAD_SHA=head, **(env or {}))
        started = time.time()
        exit_code, output = None, ""
        try:
            proc = subprocess.Popen(check["argv"], cwd=cwd, env=child, stdin=subprocess.DEVNULL, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                output, _ = proc.communicate(timeout=check["timeout_seconds"])
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                output = (proc.communicate()[0] or "") + f"\ntimed out after {check['timeout_seconds']}s"
        except OSError as error:
            output = f"could not start {check['argv'][0]}: {error.strerror or error}"
    finally:
        _cleanup(worktree, scratch)
    passed = int(exit_code in check["allowed_exit_codes"])
    cursor = con.execute(
        "INSERT INTO check_runs(task_id, kind, hub, hub_commit, definition_sha256, argv, cwd, namespace, base_sha, "
        "head_sha, exit_code, passed, output, started_at, duration) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task["id"], kind, loaded["hub"], loaded["hub_commit"], loaded["definition_sha256"],
         json.dumps(check["argv"]), check["cwd"], space["ORCHD_TEST_NAMESPACE"], task["base"], head, exit_code,
         passed, _bounded(output), started, time.time() - started))
    return con.execute("SELECT * FROM check_runs WHERE id=?", (cursor.lastrowid,)).fetchone()


def _scratch_dir():
    directory = Path(os.environ.get("ORCHD_HOME", Path.home() / ".local/share/orchd")) / "check-worktrees"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cleanup(worktree, scratch):
    if scratch is None:
        return
    subprocess.run(["git", "-C", str(worktree), "worktree", "remove", "--force", str(scratch / "tree")],
                   capture_output=True)
    shutil.rmtree(scratch, ignore_errors=True)
    subprocess.run(["git", "-C", str(worktree), "worktree", "prune"], capture_output=True)


def records(con, task_id):
    return con.execute("SELECT * FROM check_runs WHERE task_id=? ORDER BY id", (task_id,)).fetchall()


# The only gh pr create arguments a worker may pass. --head and --repo are set by orchd from the task branch and
# origin; everything else (--fill, --web, -R, -H, --template, positional values, ...) is refused before gh runs.
VALUE_FLAGS = {"--title": "title", "--body": "body", "--body-file": "body_file", "--base": "base", "--label": "label",
               "--reviewer": "reviewer", "--assignee": "assignee"}
BOOL_FLAGS = {"--draft": "draft"}
REPEATABLE = ("--label", "--reviewer", "--assignee")
ALLOWED = ", ".join([*VALUE_FLAGS, *BOOL_FLAGS])


def parse_pr_args(args):
    """The PR inputs of a `gh pr create` invocation, in order. Raises CheckError for anything not whitelisted."""
    found = {"title": None, "body": None, "base": None, "body_file": None, "labels": [], "reviewers": [],
             "assignees": [], "draft": False, "order": []}
    rest = list(args[2:])
    while rest:
        arg = rest.pop(0)
        name, equals, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if (name in BOOL_FLAGS and not equals) or name in VALUE_FLAGS:
            if name not in REPEATABLE and any(seen == name for seen, _ in found["order"]):
                raise _problem("pr", f"{name} was given more than once; give each of {ALLOWED} at most once "
                                     f"(only {', '.join(REPEATABLE)} may repeat)")
        if name in BOOL_FLAGS and not equals:
            found["draft"] = True
            found["order"].append((name, None))
        elif name in VALUE_FLAGS:
            if not equals:
                if not rest:
                    raise _problem("pr", f"{name} needs a value")
                value = rest.pop(0)
            field = VALUE_FLAGS[name]
            if field in ("label", "reviewer", "assignee"):
                found[field + "s"].append(value)
            else:
                found[field] = value
            found["order"].append((name, value))
        else:
            raise _problem("pr", f"argument {arg!r} is not allowed. gh pr create accepts only: {ALLOWED}; "
                                 "--head and --repo are set by orchd from the task branch and origin")
    given = {name for name, _ in found["order"]}
    if {"--body", "--body-file"} <= given:
        raise _problem("pr", "--body and --body-file are mutually exclusive; give one")
    found["labels"] = [part for value in found["labels"] for part in value.split(",") if part]
    return found


def _owner_repo(text):
    """(owner, repo) lowercased from owner/repo, [host/]owner/repo, a URL or scp-style remote; None if not parseable."""
    text = text.strip()
    if "://" in text:
        text = urlsplit(text).path
    elif ":" in text and "/" not in text.split(":", 1)[0]:
        text = text.split(":", 1)[1]
    parts = [p for p in text.strip("/").split("/") if p]
    if len(parts) < 2 or not (len(parts) == 2 or "." in parts[-3]):
        return None
    repo = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
    return (parts[-2].lower(), repo.lower()) if parts[-2] and repo else None


def _problem(kind, message):
    return CheckError(f"gh pr create refused by orchd ({kind}): {message}")


def _fetch_branch(worktree, branch):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")
    return subprocess.run(["git", "-C", str(worktree), "fetch", "--quiet", "origin",
                           f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], capture_output=True, text=True,
                          timeout=worker_flow.BRIEF_BUDGET, env=env)


def _repo_candidates(worktree):
    """Repo names a worktree no task owns could belong to: its directory name minus a task id, and origin's name."""
    names = {re.sub(r"-[0-9a-f]{8}$", "", Path(worktree).name)}
    remote = subprocess.run(["git", "-C", str(worktree), "config", "--get", "remote.origin.url"],
                            capture_output=True, text=True)
    origin = _owner_repo(remote.stdout) if remote.returncode == 0 else None
    if origin:
        names.add(origin[1])
    return names


def gate_worktree(con, worktree, args, refuse=None, projects=None):
    """What the worker gh wrapper calls. Returns the exact args for gh. A repo with no pipeline gets its command back
    unchanged whatever the task's state; only a configured repo refuses (closed or unknown task, wrapper error)."""
    try:
        flows = config.delivery_flows()
    except ValueError as error:
        raise CheckError(f"delivery flow configuration: {error}") from error
    resolved = str(Path(worktree).resolve())
    row = next((t for t in con.execute("SELECT * FROM tasks WHERE worktree IS NOT NULL")
                if str(Path(t["worktree"]).resolve()) == resolved), None)
    configured = row["repo"] in flows if row is not None else any(n in flows for n in _repo_candidates(resolved))
    if not configured:
        return list(args)
    if refuse:
        raise _problem("pr", refuse)
    if row is None:
        raise _problem("pr", f"no task owns worktree {resolved}, so its checks cannot be found")
    if row["status"] not in store.OPEN:
        raise _problem("pr", f"task {row['id']} is {row['status']}; a closed task cannot open a PR")
    final = gate_pr(con, row, list(args), projects)
    return list(args) if final is None else final


def gate_pr(con, task, args, projects=None):
    """Raise CheckError unless the pinned checks allow this `gh pr create`. Returns the exact gh arguments to run
    (whitelisted flags, --head and --repo set by orchd), or None when the repo has no pipeline."""
    loaded = load(task["repo"], projects)
    if loaded is None:
        return None
    checks, task_id = loaded["checks"], task["id"]
    if checks.get("preflight", {}).get("required"):
        rows = [r for r in records(con, task_id) if r["kind"] == "preflight"]
        if not rows:
            raise _problem("preflight", f"no preflight has been run; run `orchd check preflight {task_id}`")
        last = rows[-1]
        if last["definition_sha256"] != loaded["definition_sha256"] or last["base_sha"] != task["base"]:
            raise _problem("preflight", "the pipeline definition changed or the base moved since the last preflight; "
                                        f"run `orchd check preflight {task_id}` again")
        if last["head_sha"] != task["base"]:
            raise _problem("preflight", "the last preflight did not run on the task's base commit; "
                                        f"run `orchd check preflight {task_id}` again")
        if not last["passed"]:
            raise _problem("preflight", f"the last preflight did not pass (exit {last['exit_code']}); fix it and run "
                                        f"`orchd check preflight {task_id}` again. Output:\n{last['output']}")
    worktree = task["worktree"] or ""
    inputs = parse_pr_args(args)
    if inputs["body_file"] == "-":
        raise _problem("pr", "--body-file - reads stdin, which cannot be checked; write the body to a file")
    branch = task["branch"] or ""
    try:
        if _dirty(worktree):
            raise _problem("pr", "the worktree has uncommitted changes; commit them so the check sees the exact head")
        head = _git(worktree, "rev-parse", "HEAD")
        if not branch or _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") != branch:
            raise _problem("pr", f"the worktree is not on the task branch {branch}; the check would not see the PR head")
        remote = subprocess.run(["git", "-C", str(worktree), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True)  # the configured URL, not an insteadOf rewrite
        origin = _owner_repo(remote.stdout) if remote.returncode == 0 else None
        if origin is None:
            raise _problem("pr", "origin is not a recognizable GitHub remote, so the target repo cannot be set")
        base_branch = task["base_branch"]
        if not base_branch:
            raise _problem("pr", "the task has no recorded base branch, so the PR target cannot be verified; "
                                 "re-dispatch the task")
        if inputs["base"] is not None and inputs["base"] != base_branch:
            raise _problem("pr", f"--base {inputs['base']} is not the task's base branch {base_branch}; preflight ran on "
                                 f"{base_branch}, so a PR may only target it (omit --base)")
        # Refresh just these two refs first: a stale local origin/<base> must not vouch for a rewritten remote.
        base_fetch = _fetch_branch(worktree, base_branch)
        if base_fetch.returncode != 0:
            raise _problem("pr", f"could not fetch origin/{base_branch} ({worker_flow._error(base_fetch.stderr)}); "
                                 "it must exist on origin and origin must be reachable")
        head_fetch = _fetch_branch(worktree, branch)
        if head_fetch.returncode != 0 and "couldn't find remote ref" not in head_fetch.stderr:
            raise _problem("pr", f"could not fetch origin/{branch} ({worker_flow._error(head_fetch.stderr)})")
        on_origin = head_fetch.returncode == 0
        tip = _git(worktree, "rev-parse", "--verify", f"refs/remotes/origin/{base_branch}^{{commit}}")
        if subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", task["base"], tip],
                          capture_output=True).returncode != 0:
            raise _problem("pr", f"origin/{base_branch} does not contain the task's base {task['base'][:12]} that "
                                 "preflight ran on; the base was rewritten, so re-dispatch the task")
        pushed = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--verify", "-q",
                                 f"refs/remotes/origin/{branch}^{{commit}}"], capture_output=True, text=True)
        if on_origin and pushed.returncode == 0 and pushed.stdout.strip() != head:
            raise _problem("pr", f"origin/{branch} is at {pushed.stdout.strip()[:12]} but the checked head is "
                                 f"{head[:12]}; push (or fetch) so the PR head is the checked commit")
    except (OSError, subprocess.SubprocessError) as error:
        raise _problem("pr", f"cannot read the worktree: {worker_flow._error(error)}") from error
    if inputs["body_file"]:
        try:
            inputs["body"] = Path(inputs["body_file"]).read_text()
        except (OSError, UnicodeDecodeError) as error:
            raise _problem("pr", f"cannot read the body file: {error}") from error
    inputs.update(head=branch, repo=f"{origin[0]}/{origin[1]}", base=base_branch)
    order = inputs.pop("order")
    inputs.update(base_sha=task["base"], head_sha=head, task_id=task_id)
    directory = Path(os.environ.get("ORCHD_HOME", Path.home() / ".local/share/orchd")) / "pr-inputs"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{task_id}-{head[:12]}.json"
    target.write_text(json.dumps(inputs, ensure_ascii=False, indent=1))
    if "pr" in checks:
        row = run(con, task, "pr", projects, env={"ORCHD_PR_INPUT": str(target)})
        if not row["passed"]:
            raise _problem("pr", f"checks.pr failed (exit {row['exit_code']}). Output:\n{row['output']}")
    # The exact arguments gh gets: the checked body, and the task branch and origin repo as head and repo.
    final = ["pr", "create"]
    for name, value in order:
        if name == "--base":
            continue
        if name == "--body-file":
            name, value = "--body", inputs["body"]
        final += [name] if value is None else [name, value]
    return final + ["--base", base_branch, "--head", branch, "--repo", inputs["repo"]]
