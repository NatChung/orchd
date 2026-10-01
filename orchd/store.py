"""SQLite state: one row per task, plus the messages exchanged about it."""
import os
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    done_when TEXT NOT NULL,
    orch_thread TEXT NOT NULL,
    codex_bin TEXT NOT NULL,
    base TEXT,
    branch TEXT,
    worktree TEXT,
    socket TEXT,
    job_id TEXT,
    session_id TEXT,
    status TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    evidence TEXT,
    wake_error TEXT,
    created_at REAL NOT NULL,
    read_at REAL
);
CREATE TABLE IF NOT EXISTS orchs(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    model TEXT,
    socket TEXT,
    session_id TEXT,
    job_id TEXT,
    created_at REAL NOT NULL,
    stopped_at REAL
);
"""

# Columns added after v1. Nullable so old rows and old code keep working against the same DB.
TASK_COLUMNS = ("model TEXT", "model_reason TEXT", "task_type TEXT", "rework_of TEXT",
                "found_by TEXT", "outcome TEXT", "rating INTEGER")

# Message kinds the Orch reads in its inbox; the rest (dispatch, answer, close, usage) are the event log.
ORCH_KINDS = ("ack", "progress", "report", "question")

# starting -> running -> acked -> done|blocked|question -> closed; failed if launch breaks.
OPEN = ("starting", "running", "acked", "done", "blocked", "question", "failed")


def home():
    return Path(os.environ.get("ORCHD_HOME", Path.home() / ".local/share/orchd"))


def connect(path=None):
    path = Path(path or home() / "orchd.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}
    for column in TASK_COLUMNS:
        if column.split()[0] not in have:
            try:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
            except sqlite3.OperationalError:  # another process added it first
                pass
    return con


def new_task_id():
    return uuid.uuid4().hex[:8]


def create_task(con, **fields):
    now = time.time()
    fields.setdefault("status", "starting")
    fields.update(created_at=now, updated_at=now)
    cols = ",".join(fields)
    con.execute(f"INSERT INTO tasks({cols}) VALUES({','.join('?' * len(fields))})", tuple(fields.values()))
    return get_task(con, fields["id"])


def update_task(con, task_id, **fields):
    fields["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))


def get_task(con, task_id):
    row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown task {task_id}")
    return row


def open_tasks(con):
    marks = ",".join("?" * len(OPEN))
    return con.execute(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at", OPEN).fetchall()


def get_orch(con, orch_id):
    return con.execute("SELECT * FROM orchs WHERE id=?", (orch_id,)).fetchone()


def register_orch(con, id, kind, model=None, socket=None, session_id=None, job_id=None):
    con.execute("INSERT OR IGNORE INTO orchs(id,kind,model,socket,session_id,job_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (id, kind, model, socket, session_id, job_id, time.time()))
    return get_orch(con, id)


def stop_orch(con, orch_id):
    con.execute("UPDATE orchs SET stopped_at=? WHERE id=?", (time.time(), orch_id))


def add_message(con, task_id, kind, body, evidence=None):
    cur = con.execute("INSERT INTO messages(task_id,kind,body,evidence,created_at) VALUES(?,?,?,?,?)",
                      (task_id, kind, body, evidence, time.time()))
    return cur.lastrowid


def unread_for_thread(con, thread):
    return con.execute(
        "SELECT m.*, t.repo, t.title, t.status, t.model FROM messages m JOIN tasks t ON t.id=m.task_id "
        f"WHERE t.orch_thread=? AND m.read_at IS NULL AND m.kind IN ({','.join('?' * len(ORCH_KINDS))}) "
        "ORDER BY m.id", (thread, *ORCH_KINDS)).fetchall()


def notification_delivery(con, task_id):
    """Public inbox metadata only: never read/return body, evidence or raw errors.

An error prefix can contain private text too. Return only a known built-in
exception class name (including queue subprocess errors), otherwise 'unknown'.
"""
    import builtins
    import subprocess

    marks = ','.join('?' * len(ORCH_KINDS))
    where = f"task_id=? AND read_at IS NULL AND kind IN ({marks})"
    counts = con.execute(
        f"SELECT COUNT(*) AS unread, COUNT(wake_error) AS failed FROM messages WHERE {where}",
        (task_id, *ORCH_KINDS)).fetchone()
    latest = con.execute(
        "SELECT id,kind,created_at,substr(wake_error,1,instr(wake_error,':')-1) AS error_type "
        f"FROM messages WHERE {where} AND wake_error IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id, *ORCH_KINDS)).fetchone()
    failure = None
    if latest is not None:
        name = latest["error_type"]
        error_class = getattr(builtins, name, None) or getattr(subprocess, name, None)
        safe = isinstance(error_class, type) and issubclass(error_class, Exception)
        failure = dict(message_id=latest["id"], kind=latest["kind"], created_at=latest["created_at"],
                       error_type=name if safe else "unknown")
    return dict(unread_count=counts["unread"], unread_wake_failed_count=counts["failed"],
                latest_unread_wake_failure=failure)


def mark_read(con, ids):
    now = time.time()
    con.executemany("UPDATE messages SET read_at=? WHERE id=?", [(now, i) for i in ids])
