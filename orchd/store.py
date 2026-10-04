"""SQLite state: one row per task, plus the messages exchanged about it."""
import os
import fcntl
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
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
CREATE TABLE IF NOT EXISTS entries(
    id TEXT PRIMARY KEY,
    orch_id TEXT NOT NULL,
    thread_id TEXT,
    bound_at REAL NOT NULL,
    thread_seen_at REAL
);
CREATE TABLE IF NOT EXISTS entry_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    orch_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    body_bytes INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    wire_text TEXT,
    source_raw TEXT,
    source_turn TEXT,
    source_thread TEXT,
    source_item_id TEXT,
    task_id TEXT,
    source_message_id INTEGER,
    reply_to INTEGER,
    question_state TEXT,
    delivery TEXT NOT NULL,
    delivery_error TEXT,
    recipient TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    delivered_at REAL,
    read_at REAL
);
"""

# Columns added after v1. Nullable so old rows and old code keep working against the same DB.
TASK_COLUMNS = ("model TEXT", "model_reason TEXT", "task_type TEXT", "rework_of TEXT",
                "found_by TEXT", "outcome TEXT", "rating INTEGER",
                "verify TEXT", "manual_checks TEXT", "verifies TEXT")
MESSAGE_COLUMNS = ("recipient_orch TEXT", "notice_error TEXT", "notice_recipient TEXT")
ORCH_COLUMNS = ("first_seen_dead REAL", "last_verified_dead REAL", "archived_at REAL")
# entry_messages columns a DB created from an early #37 draft lacks (nullable: ALTER cannot add NOT NULL).
ENTRY_MESSAGE_COLUMNS = ("body_bytes INTEGER", "body_sha256 TEXT", "wire_text TEXT", "source_thread TEXT",
                         "source_item_id TEXT", "source_raw TEXT", "source_turn TEXT")

# Message kinds the Orch reads in its inbox; the rest (dispatch, answer, close, usage) are the event log.
ORCH_KINDS = ("ack", "progress", "report", "question", "adopt")

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
    for table, columns in (("messages", MESSAGE_COLUMNS), ("entry_messages", ENTRY_MESSAGE_COLUMNS), ("orchs", ORCH_COLUMNS)):
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in have:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
                except sqlite3.OperationalError:  # another process added it first
                    pass
    # After the columns exist: on an early-draft DB this index would fail inside SCHEMA.
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS entry_messages_source ON entry_messages(source_thread, source_item_id)")
    return con


@contextmanager
def task_delivery(con, task_ids, timeout=65):
    """Cross-process task locks for ownership changes and transport, never a SQLite write lock.

    Ordered batches cannot deadlock. The wait is bounded; process exit releases flock. Keep lock files
    (unlinking a file while another process waits on it would create two independent locks).
    """
    database = con.execute("PRAGMA database_list").fetchone()[2]
    if not database:
        raise ValueError("task delivery requires a file-backed database")
    directory = Path(database).resolve().with_name(Path(database).name + ".delivery-locks")
    directory.mkdir(exist_ok=True)
    handles = []
    deadline = time.monotonic() + timeout
    try:
        for task_id in sorted(set(task_ids)):
            handle = open(directory / hashlib.sha256(task_id.encode()).hexdigest(), "a")
            handles.append(handle)
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("task delivery busy; retry after the current notification")
                    time.sleep(0.01)
        yield
    finally:
        for handle in reversed(handles):
            handle.close()


class AdoptionMoves(list):
    """Move tuples plus the registry snapshots validated against the runtime probe."""
    def __init__(self, target, owners):
        super().__init__()
        self.target = dict(target)
        self.owners = {owner: dict(row) if row is not None else None for owner, row in owners.items()}


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


def list_orchs(con):
    """Registry inventory including owners with zero open tasks; no writes."""
    marks = ",".join("?" * len(OPEN))
    return con.execute(
        "SELECT o.*,COUNT(t.id) AS open_task_count "
        "FROM orchs o LEFT JOIN tasks t ON t.orch_thread=o.id "
        f"AND t.status IN ({marks}) GROUP BY o.id ORDER BY o.created_at,o.id", OPEN).fetchall()


def known_job_ids(con):
    """Registered Orchs and historical workers must not be called unidentified sessions."""
    return {r[0] for r in con.execute(
        "SELECT job_id FROM orchs UNION SELECT job_id FROM tasks") if r[0]}


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
        "SELECT id,kind,created_at,recipient_orch,substr(wake_error,1,instr(wake_error,':')-1) AS error_type "
        f"FROM messages WHERE {where} AND wake_error IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id, *ORCH_KINDS)).fetchone()
    failure = None
    if latest is not None:
        name = latest["error_type"]
        error_class = getattr(builtins, name, None) or getattr(subprocess, name, None)
        safe = isinstance(error_class, type) and issubclass(error_class, Exception)
        failure = dict(message_id=latest["id"], kind=latest["kind"], created_at=latest["created_at"],
                       error_type=name if safe else "unknown")
        if latest["recipient_orch"] is not None:
            failure["recipient_orch"] = latest["recipient_orch"]
    result = dict(unread_count=counts["unread"], unread_wake_failed_count=counts["failed"],
                  latest_unread_wake_failure=failure)
    # Non-inbox old-owner notices have their own delivery history. They never become unread private
    # inbox bodies for the task's new group. Failed notice INSERTs are recorded on the adopt event.
    notices = con.execute(
        "SELECT id,kind,created_at,COALESCE(notice_recipient,recipient_orch, "
        "CASE WHEN kind='adopt_notice' AND json_valid(evidence) "
        "THEN json_extract(evidence, '$.old_orch') END) AS recipient_orch, "
        "substr(COALESCE(notice_error,wake_error),1,instr(COALESCE(notice_error,wake_error),':')-1) AS error_type "
        "FROM messages WHERE task_id=? AND ((kind='adopt_notice' AND wake_error IS NOT NULL) "
        "OR notice_error IS NOT NULL) ORDER BY id DESC", (task_id,)).fetchall()
    if notices:
        notice = notices[0]
        name = notice["error_type"]
        cls = getattr(builtins, name, None) or getattr(subprocess, name, None) or getattr(sqlite3, name, None)
        safe = isinstance(cls, type) and issubclass(cls, Exception)
        result.update(notice_wake_failed_count=len(notices), latest_notice_wake_failure=dict(
            message_id=notice["id"], kind=notice["kind"], created_at=notice["created_at"],
            recipient_orch=notice["recipient_orch"], error_type=name if safe else "unknown"))
    return result


def latest_message(con, task_id, kind):
    return con.execute("SELECT * FROM messages WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (task_id, kind)).fetchone()


def move_task_orch(con, moves, to_orch):
    """Move open tasks to another Orch and log one adopt event each, all in one transaction.

    moves: [(task_id, expected_from_orch, body, evidence)]. Only tasks.orch_thread changes: messages
    (and their read_at), worktree, branch, job and worker process stay as they were. Any failure, or a task
    that closed or changed owner since the caller looked, rolls the whole batch back. Returns the adopt
    message ids in order.
    """
    marks = ",".join("?" * len(OPEN))
    with task_delivery(con, [m[0] for m in moves]), immediate(con):
        target = get_orch(con, to_orch)
        if target is None:
            raise ValueError(f"unknown orch {to_orch}; the new owner must be registered")
        if target["stopped_at"] is not None:
            raise ValueError(f"orch {to_orch} is stopped")
        if target["kind"] not in ("claude", "codex"):
            raise ValueError(f"orch {to_orch} has an unsupported kind")
        if isinstance(moves, AdoptionMoves):
            if dict(target) != moves.target:
                raise ValueError("target registry changed while adopting; nothing was moved")
            for owner, expected in moves.owners.items():
                row = get_orch(con, owner)
                if (dict(row) if row is not None else None) != expected:
                    raise ValueError("old owner registry changed while adopting; nothing was moved")
        ids = []
        now = time.time()
        for task_id, from_orch, body, evidence in moves:
            cur = con.execute(
                f"UPDATE tasks SET orch_thread=?, updated_at=? WHERE id=? AND orch_thread=? AND status IN ({marks})",
                (to_orch, now, task_id, from_orch, *OPEN))
            if cur.rowcount != 1:
                raise ValueError(f"task {task_id} changed owner or closed while adopting; nothing was moved")
            ids.append(add_message(con, task_id, "adopt", body, evidence))
            con.execute("UPDATE messages SET recipient_orch=? WHERE id=?", (to_orch, ids[-1]))
        return ids


def mark_read(con, ids):
    now = time.time()
    con.executemany("UPDATE messages SET read_at=? WHERE id=?", [(now, i) for i in ids])


# Answers to a Codex worker that is mid-turn wait here (kind=answer_queued, read_at = delivered at) until the
# Orch flushes them into the worker's next turn. Plain message rows, so no schema change.
QUEUED = "answer_queued"


@contextmanager
def immediate(con):
    """Hold SQLite's write lock for the block (short commits only; never around a spawn or a socket send)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def pending_answers(con, task_id):
    return con.execute("SELECT * FROM messages WHERE task_id=? AND kind=? AND read_at IS NULL ORDER BY id",
                       (task_id, QUEUED)).fetchall()


def pending_answer_count(con, task_id):
    """Undelivered FIFO entries (answers and followups), including closed tasks; never read their bodies."""
    return con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind=? AND read_at IS NULL",
                       (task_id, QUEUED)).fetchone()[0]
