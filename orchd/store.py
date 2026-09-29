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
"""

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


def add_message(con, task_id, kind, body, evidence=None):
    cur = con.execute("INSERT INTO messages(task_id,kind,body,evidence,created_at) VALUES(?,?,?,?,?)",
                      (task_id, kind, body, evidence, time.time()))
    return cur.lastrowid


def unread_for_thread(con, thread):
    return con.execute(
        "SELECT m.*, t.repo, t.title, t.status FROM messages m JOIN tasks t ON t.id=m.task_id "
        "WHERE t.orch_thread=? AND m.read_at IS NULL AND m.kind IN ('ack','report','question') "
        "ORDER BY m.id", (thread,)).fetchall()


def mark_read(con, ids):
    now = time.time()
    con.executemany("UPDATE messages SET read_at=? WHERE id=?", [(now, i) for i in ids])
