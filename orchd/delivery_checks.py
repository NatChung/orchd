"""Hub-defined delivery checks run by orchd (#119): a preflight and a check before `gh pr create`.

The definition is `docs/development/delivery.pipeline.json` at the Hub's fetched origin/HEAD commit (see
docs/design/staged-pipeline.md). Only `checks` is read here; stages belong to the staged pipeline (#121)."""
import json
import os
import re
import signal
import subprocess
import time
from hashlib import sha256
from pathlib import Path

from . import config, paths, worker_flow
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
    return {"hub": hub, "hub_commit": commit, "definition_sha256": sha256(blob).hexdigest(),
            "checks": definition["checks"]}


def _git(worktree, *args):
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, check=True).stdout.strip()


def _bounded(text):
    text = redact(text)
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + f"\n[output truncated at {OUTPUT_LIMIT} characters]"


def run(con, task, kind, projects=None, env=None):
    """Run one pinned check in the task's worktree without a shell and record it. Returns the stored row."""
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
    cwd = (worktree / check["cwd"]).resolve()
    if worktree.resolve() not in (cwd, *cwd.parents):
        raise CheckError(f"checks.{kind}.cwd leaves the worktree")
    try:
        head = _git(worktree, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as error:
        raise CheckError(f"cannot read the worktree's HEAD: {worker_flow._error(error)}") from error
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
    passed = int(exit_code in check["allowed_exit_codes"])
    cursor = con.execute(
        "INSERT INTO check_runs(task_id, kind, hub, hub_commit, definition_sha256, argv, cwd, namespace, base_sha, "
        "head_sha, exit_code, passed, output, started_at, duration) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task["id"], kind, loaded["hub"], loaded["hub_commit"], loaded["definition_sha256"],
         json.dumps(check["argv"]), check["cwd"], space["ORCHD_TEST_NAMESPACE"], task["base"], head, exit_code,
         passed, _bounded(output), started, time.time() - started))
    return con.execute("SELECT * FROM check_runs WHERE id=?", (cursor.lastrowid,)).fetchone()


def records(con, task_id):
    return con.execute("SELECT * FROM check_runs WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
