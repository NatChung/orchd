#!/usr/bin/env python3
"""Verify Desktop, Claude Orch and a Claude worker on a dedicated macOS session."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


REPO = Path(__file__).resolve().parents[1]


def command(*args, env, cwd=None):
    return subprocess.run(args, env=env, cwd=cwd, check=True, capture_output=True,
                          text=True, timeout=150).stdout


def wait_for(label, check, timeout=150):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = check()
        if result:
            print(f"Verified: {label}", flush=True)
            return result
        time.sleep(2)
    raise RuntimeError(f"Timed out: {label}")


def query(db, sql, *args):
    if not db.is_file():
        return []
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro")) as con:
        return con.execute(sql, args).fetchall()


def desktop_reply(codex_home, thread, wire, final):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", thread):
        return False
    saw_wire = False
    for path in sorted((codex_home / "sessions").glob(f"**/rollout-*-{thread}*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload", {})
                if event.get("type") == "event_msg" and payload.get("type") == "user_message":
                    saw_wire = saw_wire or payload.get("message") == wire
                if event.get("type") == "response_item" and payload.get("role") == "user":
                    message = "".join(item.get("text", "") for item in payload.get("content", []))
                    saw_wire = saw_wire or message == wire
                if not saw_wire:
                    continue
                if event.get("type") == "event_msg" and payload.get("type") == "agent_message":
                    if final in (payload.get("message") or ""):
                        return True
                if event.get("type") == "response_item" and payload.get("role") == "assistant":
                    if any(final in (item.get("text") or "") for item in payload.get("content", [])):
                        return True
    return False


def fixture(root, env):
    projects = root / "projects"
    origin, repo = projects / "origin.git", projects / "desktop-smoke"
    projects.mkdir()
    command("git", "init", "--bare", "--initial-branch=main", str(origin), env=env)
    command("git", "clone", str(origin), str(repo), env=env)
    (repo / "README.md").write_text("Synthetic Desktop smoke fixture.\n", encoding="utf-8")
    command("git", "add", "README.md", cwd=repo, env=env)
    command("git", "-c", "user.name=Desktop Smoke", "-c", "user.email=smoke@example.invalid",
            "commit", "-m", "Seed test repository", cwd=repo, env=env)
    command("git", "push", "origin", "main", cwd=repo, env=env)
    command("git", "remote", "set-head", "origin", "-a", cwd=repo, env=env)
    return repo


def environment(root, profile):
    keep = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TMPDIR")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(ORCHD_ROOT=str(root), ORCHD_HOME=str(root / "state"),
               ORCHD_CONFIG_DIR=str(root / "config"), ORCHD_PROJECTS=str(root / "projects"),
               ORCHD_EXECUTABLE=str(REPO / "bin/orchd"), CLAUDE_CONFIG_DIR=str(profile),
               CODEX_HOME=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               PYTHONDONTWRITEBYTECODE="1")
    auth = subprocess.run(["claude", "auth", "status", "--json"], env=env, capture_output=True,
                          text=True, timeout=20)
    if auth.returncode or not json.loads(auth.stdout).get("loggedIn"):
        raise RuntimeError("Dedicated Claude profile is signed out")
    return env


def configure_claude(profile, folders):
    config = profile / ".claude.json"
    data = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    projects = data.setdefault("projects", {})
    for folder in folders:
        projects[str(folder)] = {"hasTrustDialogAccepted": True}
    with tempfile.NamedTemporaryFile(mode="w", dir=profile, delete=False, encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(stream.name, config)


def desktop_action(driver, action, project, text, env):
    if driver:
        command(str(driver), action, str(project), text, env=env)
    else:
        print(f"{action}: {text}", flush=True)


def roundtrip(root, env, orchd, db, driver):
    print("Starting isolated Claude Orch and binding Desktop entry", flush=True)
    owner = json.loads(command(*orchd, "binding", env=env))["orch_id"]
    project = root / "interface"
    desktop_action(driver, "open", project,
                   f"Open {project} as a new Desktop project chat with interface permissions. Request status.", env)
    [(thread,)] = wait_for("isolated Desktop thread bound to the test Orch", lambda: query(
        db, "SELECT thread_id FROM entries WHERE id='desktop' AND orch_id=? AND thread_id IS NOT NULL", owner
    ), timeout=480)

    nonce = uuid.uuid4().hex[:12]
    title, question, final = f"Desktop smoke {nonce}", f"SMOKE-QUESTION-{nonce}", f"SMOKE-DONE-{nonce}"
    desktop_action(driver, "send", project, f"""Pass this synthetic test to the Orch. Dispatch exactly one Sonnet investigation worker in repository desktop-smoke
with title "{title}". No repository edits, pushes, or external messages. Have the worker ack and ask one A/B
question containing "{question}". Use ask_nat to quote that worker question to me. After I answer, use answer
with my actual entry_reply_id. The worker must report done with "{final}" only after receiving my orchd answer.
Send that marker in this chat only after reading the real worker report.""", env)
    wait_for("new Desktop instruction reached Orch inbox", lambda: query(db, """
        SELECT id FROM entry_messages WHERE entry_id='desktop' AND orch_id=? AND source_thread=?
          AND direction='in' AND read_at IS NOT NULL AND instr(body,?)>0
    """, owner, thread, title))
    [(task,)] = wait_for("worker dispatch", lambda: query(
        db, "SELECT id FROM tasks WHERE title=? AND orch_thread=? AND repo='desktop-smoke'", title, owner))
    [(qid,)] = wait_for("worker question delivered to original chat", lambda: query(db, """
        SELECT e.id FROM entry_messages e JOIN messages m ON m.id=e.source_message_id
        WHERE e.entry_id='desktop' AND e.orch_id=? AND e.task_id=? AND e.kind='question'
          AND e.delivery='delivered' AND e.recipient=? AND instr(e.body,?)>0
          AND m.task_id=e.task_id AND m.kind='question' AND instr(m.body,?)>0
    """, owner, task, thread, question, question))
    answer = f"{qid} SMOKE-ANSWER-{nonce} A"
    desktop_action(driver, "send", project, answer, env)
    [(reply,)] = wait_for("verbatim answer linked to the worker", lambda: query(db, """
        SELECT e.id FROM entry_messages e JOIN messages m ON m.entry_reply_id=e.id
        WHERE e.reply_to=? AND e.kind='reply' AND e.source_thread=? AND e.body=?
          AND m.kind='answer' AND m.task_id=? AND m.body=e.body
    """, qid, thread, answer, task))
    wait_for("worker completion report", lambda: query(db, """
        SELECT id FROM messages WHERE task_id=? AND kind='report' AND body LIKE 'done:%' AND instr(body,?)>0
    """, task, final))
    [(wire,)] = wait_for("Orch result queued to original chat", lambda: query(db, """
        SELECT wire_text FROM entry_messages WHERE orch_id=? AND entry_id='desktop' AND direction='out'
          AND kind='message' AND delivery='delivered' AND recipient=? AND instr(body,?)>0
    """, owner, thread, final))
    wait_for("Desktop assistant renders result in original thread",
             lambda: desktop_reply(Path(env["CODEX_HOME"]), thread, wire, final))
    if query(db, "SELECT id FROM tasks WHERE orch_thread=?", owner) != [(task,)]:
        raise RuntimeError("Expected exactly one task")
    print(f"PASS thread={thread} orch={owner} task={task} question={qid} answer={reply}", flush=True)


def cleanup(db, env, orchd):
    task_ids = query(db, "SELECT id FROM tasks WHERE closed_at IS NULL")
    orch_ids = query(db, "SELECT id FROM orchs WHERE stopped_at IS NULL")
    failures = []
    for args in [("close", task) for task, in task_ids] + [("orch", "stop", orch) for orch, in orch_ids]:
        try:
            command(*orchd, *args, env=env)
        except (OSError, subprocess.SubprocessError):
            failures.append(args[-1])
    if failures:
        raise RuntimeError("Could not clean up isolated resources: " + ", ".join(failures))


def main():
    value = os.environ.get("ORCHD_DESKTOP_E2E_CLAUDE_CONFIG")
    if sys.platform != "darwin" or not value:
        sys.exit("Requires macOS Desktop and ORCHD_DESKTOP_E2E_CLAUDE_CONFIG")
    profile = Path(value).resolve()
    if not Path(value).is_absolute() or profile == (Path.home() / ".claude").resolve():
        sys.exit("Use a separate signed-in Claude profile")
    driver_path = os.environ.get("ORCHD_DESKTOP_E2E_DRIVER")
    driver = Path(driver_path) if driver_path else None
    if driver and (not driver.is_absolute() or not os.access(driver, os.X_OK)):
        sys.exit("ORCHD_DESKTOP_E2E_DRIVER must identify an executable absolute path")
    with tempfile.TemporaryDirectory(prefix="orchd-desktop-e2e-") as folder:
        root = Path(folder).resolve()
        try:
            env = environment(root, profile)
            repo = fixture(root, env)
            orchd = (sys.executable, str(REPO / "bin/orchd"))
            db = root / "state/orchd.db"
            command(*orchd, "init", "--no-trust", env=env)
            configure_claude(profile, [root / "home", repo])
            # Desktop MCP is launched by the app and does not inherit this script's environment.
            config = root / "interface" / ".codex" / "config.toml"
            with config.open("a", encoding="utf-8") as stream:
                for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
                    stream.write(f"{key} = {json.dumps(env[key], ensure_ascii=False)}\n")
            try:
                roundtrip(root, env, orchd, db, driver)
            finally:
                cleanup(db, env, orchd)
        except subprocess.CalledProcessError as exc:
            sys.exit(f"{Path(exc.cmd[0]).name} subprocess failed (exit {exc.returncode})")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            sys.exit(str(exc))


if __name__ == "__main__":
    main()
