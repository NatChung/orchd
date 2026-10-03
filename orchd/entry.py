"""Desktop entry (issue #37): a Codex Desktop thread that relays Nat's text to one fixed Claude Orch and back.

No body ever travels as a tool argument typed by the entry model (a probe showed it rewrites long text):
Nat's text is read from the entry thread's Codex rollout, and the Orch's text reaches the Desktop thread
through `codex queue`, which was measured byte-exact. Every row keeps the body with its UTF-8 length and
sha256, plus the task/question/reply links, so nobody has to guess which question an answer belongs to.

Delivery: pending -> sending -> delivered | failed. A row left in sending (the transport ran but its receipt
could not be written) is uncertain and is never resent automatically. A queued question waits as held until
it becomes the current one. delivered means it reached the Orch's socket, the Desktop queue or a tool
result; it never means read by Nat, and never approval.
"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from . import store
from .orch_health import owner_health

DEFAULT_ENTRY = "desktop"
RETRYABLE = ("pending", "failed")


class SourceNotReady(Exception):
    pass


def digest(text):
    raw = text.encode()
    return len(raw), hashlib.sha256(raw).hexdigest()


def get_entry(con, entry_id):
    row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        raise ValueError(f"entry {entry_id} is not bound to an Orch; Nat binds it with `orchd entry-bind ORCH_ID`")
    return row


def reachability(con, rt, orch_id):
    """Q1: a stopped or confirmed-dead Orch is offline; nothing here ever starts or replaces one."""
    orch = store.get_orch(con, orch_id)
    if orch is None:
        return {"state": "dead", "reason": "orch_unregistered"}
    if orch["stopped_at"] is not None:
        return {"state": "dead", "reason": "orch_stopped"}
    try:
        jobs = rt.live_jobs()
    except Exception:
        jobs = None
    return owner_health(orch, jobs)


def bind(con, rt, orch_id, entry_id=DEFAULT_ENTRY, force=False):
    """Operator-run. Binds the entry to one Claude Orch; rebinding to another Orch needs force."""
    orch = store.get_orch(con, orch_id)
    if orch is None:
        raise ValueError(f"unknown orch {orch_id}")
    if orch["kind"] != "claude":
        raise ValueError(f"orch {orch_id} is {orch['kind']}; the entry binds only a Claude Orch started by `orchd orch`")
    health = reachability(con, rt, orch_id)
    if health["state"] == "dead":
        raise ValueError(f"orch {orch_id} is offline ({health['reason']}); start one with `orchd orch` first")
    now = time.time()
    with store.immediate(con):
        row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is not None and row["orch_id"] != orch_id and not force:
            raise ValueError(f"entry {entry_id} is bound to {row['orch_id']}; rebinding it needs --force")
        if row is None:
            con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES(?,?,?)", (entry_id, orch_id, now))
        elif row["orch_id"] != orch_id:
            con.execute("UPDATE entries SET orch_id=?, bound_at=? WHERE id=?", (orch_id, now, entry_id))
    try:  # best effort: the Orch also learns this from its first [orchd entry] wake
        rt.send_uds(orch["socket"], orch["session_id"],
                    f"[orchd entry] Desktop entry {entry_id} is now bound to you. Nat's messages arrive as "
                    "[orchd entry] wakes: read them with entry_inbox. Reply with send_to_nat; ask Nat with ask_nat "
                    "(one question at a time, the rest queue). Forward Nat's reply to a worker with "
                    "answer(task_id, entry_reply_id=...).")
        notice = "sent"
    except Exception as error:
        notice = f"failed: {type(error).__name__}"
    return dict(entry_id=entry_id, orch_id=orch_id, model=orch["model"], orch_health=health, notice=notice)


def touch(con, entry_id, thread):
    """Record the Desktop thread now calling as the entry's thread; a different one means the entry reopened."""
    if not thread:
        raise ValueError("the entry needs the caller's thread id")
    with store.immediate(con):
        entry = get_entry(con, entry_id)
        con.execute("UPDATE entries SET thread_id=?, thread_seen_at=? WHERE id=?", (thread, time.time(), entry_id))
    return get_entry(con, entry_id), entry["thread_id"] is not None and entry["thread_id"] != thread


def _row(con, message_id):
    return con.execute("SELECT * FROM entry_messages WHERE id=?", (message_id,)).fetchone()


def _insert(con, entry, direction, kind, body, **fields):
    size, sha = digest(body)
    fields.setdefault("delivery", "pending")
    fields.update(entry_id=entry["id"], orch_id=entry["orch_id"], direction=direction, kind=kind, body=body,
                  body_bytes=size, body_sha256=sha, created_at=time.time())
    cur = con.execute(f"INSERT INTO entry_messages({','.join(fields)}) VALUES({','.join('?' * len(fields))})",
                      tuple(fields.values()))
    return cur.lastrowid


def current_question(con, entry):
    return con.execute("SELECT * FROM entry_messages WHERE entry_id=? AND orch_id=? AND kind='question' "
                       "AND question_state='current' ORDER BY id LIMIT 1", (entry["id"], entry["orch_id"])).fetchone()


def queued_questions(con, entry):
    return con.execute("SELECT * FROM entry_messages WHERE entry_id=? AND orch_id=? AND kind='question' "
                       "AND question_state='queued' ORDER BY id", (entry["id"], entry["orch_id"])).fetchall()


def question_wire(question_id, orch_id, body, task_id, queued):
    head = f"[orchd question {question_id}] from Orch {orch_id}"
    if task_id:
        head += f", task {task_id}"
    if queued:
        head += f"; {queued} more queued"
    return f"{head}. Nat's next message answers it: relay it with reply_to={question_id}.\n\n{body}"


def message_wire(message_id, orch_id, body):
    return f"[orchd message {message_id}] from Orch {orch_id}\n\n{body}"


def _make_current(con, entry, row):
    queued = len(queued_questions(con, entry)) - (1 if row["question_state"] == "queued" else 0)
    con.execute("UPDATE entry_messages SET question_state='current', delivery='pending', wire_text=? WHERE id=?",
                (question_wire(row["id"], row["orch_id"], row["body"], row["task_id"], queued), row["id"]))


def _send(con, rows, recipient, transport):
    """Send rows in order; stop at the first failure so later rows never overtake it."""
    result = {}
    for row in rows:
        con.execute("UPDATE entry_messages SET delivery='sending', attempts=attempts+1, recipient=?, "
                    "delivery_error=NULL WHERE id=?", (recipient, row["id"]))
        try:
            transport(row)
        except Exception as error:
            try:
                con.execute("UPDATE entry_messages SET delivery='failed', delivery_error=? WHERE id=?",
                            (f"{type(error).__name__}: {error}"[:500], row["id"]))
                result[row["id"]] = "failed"
            except Exception:
                result[row["id"]] = "uncertain"
            break
        try:
            con.execute("UPDATE entry_messages SET delivery='delivered', delivered_at=? WHERE id=?",
                        (time.time(), row["id"]))
        except Exception:
            result[row["id"]] = "uncertain"
            break
        result[row["id"]] = "delivered"
    return result


def _lock(con, entry_id):
    return store.task_delivery(con, [f"entry:{entry_id}"])


def inbound_wake(row):
    what = (f"reply to question {row['reply_to']}" + (f" (task {row['task_id']})" if row["task_id"] else "")
            if row["kind"] == "reply" else "message")
    return (f"[orchd entry] Nat {what}, entry message {row['id']}, {row['body_bytes']} bytes — "
            "請呼叫 orchd 的 entry_inbox 工具讀取原文。")


def deliver_inbound(con, rt, entry_id):
    """Wake the bound Orch for each stored Nat message; an offline Orch keeps them pending (Q1)."""
    with _lock(con, entry_id):
        entry = get_entry(con, entry_id)
        rows = con.execute("SELECT * FROM entry_messages WHERE entry_id=? AND orch_id=? AND direction='in' "
                           "AND read_at IS NULL AND delivery IN ('pending','failed') ORDER BY id",
                           (entry_id, entry["orch_id"])).fetchall()
        if not rows:
            return {}
        health = reachability(con, rt, entry["orch_id"])
        if health["state"] == "dead":
            con.executemany("UPDATE entry_messages SET delivery_error=? WHERE id=?",
                            [(f"orch offline: {health['reason']}", r["id"]) for r in rows])
            return {r["id"]: "not_delivered" for r in rows}
        orch = store.get_orch(con, entry["orch_id"])
        return _send(con, rows, orch["id"],
                     lambda row: rt.send_uds(orch["socket"], orch["session_id"], inbound_wake(row)))


def deliver_outbound(con, rt, entry_id):
    """Queue the Orch's messages and the current question into the entry's Desktop thread, oldest first."""
    with _lock(con, entry_id):
        entry = get_entry(con, entry_id)
        if not entry["thread_id"]:
            return {}  # no Desktop thread has called yet; status() will hand them over
        rows = con.execute("SELECT * FROM entry_messages WHERE entry_id=? AND direction='out' "
                           "AND delivery IN ('pending','failed') ORDER BY id", (entry_id,)).fetchall()
        thread = entry["thread_id"]
        return _send(con, rows, thread, lambda row: rt.wake_orch(rt.codex, thread, row["wire_text"]))


def _source(rt, thread, turn_id):
    messages = rt.codex_user_messages(thread)
    if turn_id:
        messages = [m for m in messages if m["turn_id"] == turn_id]
    if not messages or not messages[-1].get("item_id"):
        raise SourceNotReady
    return messages[-1]


def _public(row, body=False):
    data = dict(message_id=row["id"], kind=row["kind"], body_bytes=row["body_bytes"],
                body_sha256=row["body_sha256"], delivery=_state(row), task_id=row["task_id"])
    if row["reply_to"] is not None:
        data["reply_to"] = row["reply_to"]
    if row["delivery_error"]:
        data["delivery_error"] = row["delivery_error"]
    if body:
        data["body"] = row["body"]
    return data


def _state(row):
    return "uncertain" if row["delivery"] == "sending" else row["delivery"]


def relay(con, rt, entry_id, thread, turn_id=None, reply_to=None):
    """Entry tool: relay Nat's latest message in this thread, read from the rollout, to the bound Orch."""
    entry, resumed = touch(con, entry_id, thread)
    try:
        source = _source(rt, thread, turn_id)
    except SourceNotReady:
        return dict(status="source_not_ready", resumed=resumed,
                    note="Nat's message is not in this thread's saved history yet; nothing was relayed. "
                         "Call relay again.")
    existing = con.execute("SELECT * FROM entry_messages WHERE source_thread=? AND source_item_id=?",
                           (thread, source["item_id"])).fetchone()
    if existing is not None:
        return dict(_public(existing), status="duplicate", resumed=resumed,
                    note=f"this message was already relayed as entry message {existing['id']}; nothing new was "
                         "sent. If Nat just wrote something new, call relay again shortly.")
    injected = con.execute("SELECT id FROM entry_messages WHERE direction='out' AND wire_text=? LIMIT 1",
                           (source["text"],)).fetchone()
    if injected is not None:
        raise ValueError(f"the latest user message in this thread is orchd's own message {injected['id']}, "
                         "not Nat's; nothing was relayed")
    if reply_to is not None and not isinstance(reply_to, int):
        raise ValueError("reply_to must be a question id number")
    note = None
    try:
        with store.immediate(con):
            entry = get_entry(con, entry_id)
            fields = dict(source_thread=thread, source_item_id=source["item_id"])
            if reply_to is not None:
                question = _row(con, reply_to)
                if question is None or question["entry_id"] != entry_id or question["kind"] != "question":
                    raise ValueError(f"{reply_to} is not a question of this entry; nothing was relayed")
                if question["orch_id"] != entry["orch_id"]:
                    raise ValueError(f"question {reply_to} belongs to an earlier binding; nothing was relayed")
                if question["question_state"] == "answered":
                    answer = con.execute("SELECT id FROM entry_messages WHERE kind='reply' AND reply_to=? "
                                         "ORDER BY id LIMIT 1", (reply_to,)).fetchone()
                    raise ValueError(f"question {reply_to} was already answered by entry message "
                                     f"{answer['id'] if answer else '?'}; nothing was relayed. Relay it without "
                                     "reply_to if it is a new message")
                if question["question_state"] != "current":
                    raise ValueError(f"question {reply_to} has not been asked yet; nothing was relayed")
                con.execute("UPDATE entry_messages SET question_state='answered' WHERE id=?", (reply_to,))
                fields.update(reply_to=reply_to, task_id=question["task_id"],
                              source_message_id=question["source_message_id"])
                message_id = _insert(con, entry, "in", "reply", source["text"], **fields)
                following = queued_questions(con, entry)
                if following:
                    _make_current(con, entry, following[0])
            else:
                message_id = _insert(con, entry, "in", "message", source["text"], **fields)
                current = current_question(con, entry)
                if current is not None:
                    note = (f"question {current['id']} is still open; this was relayed as a new message, "
                            "not as its answer")
    except sqlite3.IntegrityError:  # a concurrent relay stored the same source item first
        existing = con.execute("SELECT * FROM entry_messages WHERE source_thread=? AND source_item_id=?",
                               (thread, source["item_id"])).fetchone()
        return dict(_public(existing), status="duplicate", resumed=resumed)
    sent = deliver_inbound(con, rt, entry_id).get(message_id)
    deliver_outbound(con, rt, entry_id)  # a reply may have promoted the next question
    row = _row(con, message_id)
    status = sent or ("delivered" if row["read_at"] else _state(row))
    health = reachability(con, rt, entry["orch_id"])
    result = dict(_public(row), status=status, orch_id=entry["orch_id"], orch_health=health, resumed=resumed)
    if status == "not_delivered":
        result["note"] = (f"Orch {entry['orch_id']} is offline ({health['reason']}): the message could not be "
                          "passed on and is kept; it is sent when that Orch is back. No Orch was started.")
    elif status in ("failed", "pending"):
        result["note"] = ("stored but not delivered to the Orch yet (it waits behind any earlier undelivered "
                          "message); it is retried on the next relay or status")
    elif status == "uncertain":
        result["note"] = ("the Orch may have been notified but the receipt was not saved; it is not resent "
                          "automatically, check entry-status")
    elif note:
        result["note"] = note
    return result


def status(con, rt, entry_id, thread):
    """Entry tool: binding, the current question in full, anything not yet handed over, and retries."""
    entry, resumed = touch(con, entry_id, thread)
    deliver_inbound(con, rt, entry_id)
    deliver_outbound(con, rt, entry_id)
    entry = get_entry(con, entry_id)
    with store.immediate(con):
        undelivered = con.execute("SELECT * FROM entry_messages WHERE entry_id=? AND direction='out' "
                                  "AND delivery IN ('pending','failed','sending') ORDER BY id",
                                  (entry_id,)).fetchall()
        handed = [r["id"] for r in undelivered if r["delivery"] != "sending"]
        con.executemany("UPDATE entry_messages SET delivery='delivered', delivered_at=?, recipient=? WHERE id=?",
                        [(time.time(), f"status:{thread}", i) for i in handed])
    current = current_question(con, entry)
    inbound = con.execute("SELECT * FROM entry_messages WHERE entry_id=? AND direction='in' AND read_at IS NULL "
                          "AND delivery<>'delivered' ORDER BY id", (entry_id,)).fetchall()
    return dict(entry_id=entry_id, orch_id=entry["orch_id"], orch_health=reachability(con, rt, entry["orch_id"]),
                resumed=resumed,
                current_question=dict(_public(current, body=True), wire_text=current["wire_text"]) if current else None,
                queued_questions=len(queued_questions(con, entry)),
                not_yet_delivered_to_orch=[_public(r) for r in inbound],
                from_orch=[dict(_public(r, body=True), wire_text=r["wire_text"]) for r in undelivered])


# -- Orch side --------------------------------------------------------------------------------------------

def _entry_of(con, orch_id):
    rows = con.execute("SELECT * FROM entries WHERE orch_id=? ORDER BY id", (orch_id,)).fetchall() if orch_id else []
    if not rows:
        raise ValueError("no Desktop entry is bound to this Orch")
    if len(rows) > 1:
        raise ValueError(f"several entries are bound to this Orch ({', '.join(r['id'] for r in rows)})")
    return rows[0]


def _questions(con, entry):
    current = current_question(con, entry)
    return dict(current_question=dict(id=current["id"], task_id=current["task_id"]) if current else None,
                queued_questions=[dict(id=r["id"], task_id=r["task_id"]) for r in queued_questions(con, entry)])


def inbox(con, orch_id):
    """Orch tool: Nat's unread messages and replies, verbatim, with their question/task links."""
    entry = _entry_of(con, orch_id)
    now = time.time()
    with store.immediate(con):
        rows = con.execute("SELECT * FROM entry_messages WHERE entry_id=? AND orch_id=? AND direction='in' "
                           "AND read_at IS NULL ORDER BY id", (entry["id"], orch_id)).fetchall()
        con.executemany("UPDATE entry_messages SET read_at=?, delivery='delivered', "
                        "delivered_at=COALESCE(delivered_at, ?) WHERE id=?", [(now, now, r["id"]) for r in rows])
    messages = []
    for r in rows:
        data = _public(r, body=True)
        data.pop("delivery")
        data.pop("delivery_error", None)
        if r["source_message_id"] is not None:
            data["worker_question_message_id"] = r["source_message_id"]
        messages.append(data)
    return dict(entry_id=entry["id"], messages=messages, **_questions(con, entry))


def _deliver_result(con, rt, entry, message_id):
    deliver_outbound(con, rt, entry["id"])
    row = _row(con, message_id)
    result = _public(row)
    if row["delivery"] == "pending" and not get_entry(con, entry["id"])["thread_id"]:
        result["note"] = "no Desktop entry thread has called yet; Nat sees it when the entry calls status"
    return result


def send_to_nat(con, rt, orch_id, text):
    """Orch tool: a plain message to Nat, queued into the entry's Desktop thread byte for byte."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("send_to_nat needs non-empty text")
    entry = _entry_of(con, orch_id)
    with store.immediate(con):
        message_id = _insert(con, entry, "out", "message", text)
        con.execute("UPDATE entry_messages SET wire_text=? WHERE id=?", (message_wire(message_id, orch_id, text),
                                                                          message_id))
    return dict(_deliver_result(con, rt, entry, message_id), **_questions(con, entry))


def ask_nat(con, rt, orch_id, text=None, task_id=None, quote_worker_question=False):
    """Orch tool: one question to Nat. Only one is open at a time; the rest wait in order (Q2)."""
    entry = _entry_of(con, orch_id)
    source_message_id = None
    parts = [text] if isinstance(text, str) and text.strip() else []
    if task_id is not None:
        task = store.get_task(con, task_id)
        if task["orch_thread"] != orch_id:
            raise ValueError(f"task {task_id} belongs to another Orch")
        latest = store.latest_message(con, task_id, "question")
        source_message_id = latest["id"] if latest is not None else None
        if quote_worker_question:
            if latest is None:
                raise ValueError(f"task {task_id} has no worker question to quote")
            parts.append(f"[worker question {latest['id']}, task {task_id}, verbatim]\n{latest['body']}")
    elif quote_worker_question:
        raise ValueError("quote_worker_question needs task_id")
    if not parts:
        raise ValueError("ask_nat needs text, or quote_worker_question with task_id")
    body = "\n\n".join(parts)
    with store.immediate(con):
        busy = current_question(con, entry) is not None
        question_id = _insert(con, entry, "out", "question", body, task_id=task_id,
                              source_message_id=source_message_id,
                              question_state="queued" if busy else "current", delivery="held" if busy else "pending")
        if not busy:
            _make_current(con, entry, _row(con, question_id))
    result = dict(_deliver_result(con, rt, entry, question_id), question_id=question_id,
                  state=_row(con, question_id)["question_state"], **_questions(con, entry))
    if busy:
        result["note"] = "another question is open; this one waits and is sent after Nat answers the earlier ones"
    return result


def reply_text(con, orch_id, reply_id, task_id):
    """The stored verbatim body of Nat's reply, for answer(task_id, entry_reply_id=...); links must match."""
    entry = _entry_of(con, orch_id)
    row = _row(con, reply_id)
    if row is None or row["entry_id"] != entry["id"] or row["direction"] != "in":
        raise ValueError(f"{reply_id} is not a message from Nat on this Orch's entry")
    if row["kind"] != "reply":
        raise ValueError(f"entry message {reply_id} is not a reply to a question; ask again with "
                         "ask_nat(task_id=...) so Nat's answer carries reply_to")
    if row["task_id"] != task_id:
        raise ValueError(f"entry message {reply_id} answers question {row['reply_to']} of task {row['task_id']}, "
                         f"not task {task_id}")
    return row["body"]


def snapshot(con, rt, entry_id=DEFAULT_ENTRY):
    """Read-only operator view (CLI): no delivery, no thread update, no bodies."""
    entry = get_entry(con, entry_id)
    counts = con.execute("SELECT direction, kind, CASE WHEN delivery='sending' THEN 'uncertain' ELSE delivery END AS "
                         "state, COUNT(*) AS n FROM entry_messages WHERE entry_id=? GROUP BY 1,2,3",
                         (entry_id,)).fetchall()
    return dict(entry_id=entry_id, orch_id=entry["orch_id"], thread_id=entry["thread_id"],
                orch_health=reachability(con, rt, entry["orch_id"]), **_questions(con, entry),
                counts=[dict(r) for r in counts])


# -- desk: the packaged entry folder (`orchd desk`) --------------------------------------------------------

DESK_MODEL = "gpt-6.1-sol"  # restate test: Sol 43/43 at medium, 20/20 at low; Luna 22/40 (docs/entry.md)

DESK_AGENTS = """# desk

這裡是 Nat 的 desk：Nat 在這裡交辦、聽回報，後面是一個固定的 orchd Orch。你只負責傳話。

- 對話一開始或重開時，先呼叫 orchd_entry 的 `status`。
- Nat 說話後呼叫 `relay`；只有在回答目前開著的 `[orchd question N]` 時才帶 `reply_to=N`。
  orchd 會自己從對話紀錄讀 Nat 的原文，你不要重打。
- `[orchd message N]`／`[orchd question N]` 是 Orch 給 Nat 的話，已經原樣顯示在畫面上。
  Nat 用語音聽時，逐字唸出內文；不縮短、不摘要、不改寫。
- 不分類、不排程、不替 Nat 決定、不開始任何工作。Nat 批准的是畫面上的原文，不是你唸的版本。
- 只回報 orchd 回傳的狀態；delivered 不代表 Nat 已讀或已同意。
"""


def desk_home():
    return Path(os.environ.get("ORCHD_DESK_HOME", Path.home() / "projects" / "desk"))


def desk_config(orchd_bin):
    lines = [f'model = "{DESK_MODEL}"', 'model_reasoning_effort = "low"', 'default_permissions = ":read-only"',
             'approval_policy = "never"', 'approvals_reviewer = "user"', "allow_login_shell = false", "",
             "[mcp_servers.orchd_entry]", 'command = "/usr/bin/python3"',
             f'args = [{json.dumps(str(orchd_bin))}, "mcp", "--role", "entry"]']
    if os.environ.get("ORCHD_HOME"):
        lines.append(f'env = {{ ORCHD_HOME = {json.dumps(os.environ["ORCHD_HOME"])} }}')
    return "\n".join(lines) + "\n"


def _write_managed(path, text):
    """Create a file orchd owns; one that differs (hand-edited or older) is kept and reported, never overwritten."""
    if path.exists():
        return "unchanged" if path.read_text() == text else "kept (differs from orchd's version; delete it to regenerate)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return "created"


def codex_trusted(home):
    """True/False from ~/.codex/config.toml, None when it cannot be read. Never writes it."""
    try:
        import tomllib
    except ImportError:
        return None
    cfg = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        projects = tomllib.loads(cfg.read_text()).get("projects") or {}
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    entry = projects.get(str(Path(home).resolve())) or projects.get(str(home))
    return isinstance(entry, dict) and entry.get("trust_level") == "trusted"


def desk(con, rt, start_orch, orchd_bin, new=False, entry_id=DEFAULT_ENTRY):
    """Operator: prepare the desk folder and make sure the entry is bound to a live Claude Orch.

    Reuses the bound Orch unless it is confirmed dead (Q3). A dead one is never replaced silently (Q1): that needs
    new=True, which starts a new Orch and rebinds; the old Orch's open questions stay with it. With no binding yet,
    it starts one Orch and binds it.
    """
    home = desk_home()
    files = {".codex/config.toml": _write_managed(home / ".codex" / "config.toml", desk_config(orchd_bin)),
             "AGENTS.md": _write_managed(home / "AGENTS.md", DESK_AGENTS)}
    row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    health = reachability(con, rt, row["orch_id"]) if row is not None else None
    if row is not None and health["state"] != "dead" and not new:
        orch_id, started = row["orch_id"], False
    else:
        if row is not None and not new:
            entry = get_entry(con, entry_id)
            open_questions = len(queued_questions(con, entry)) + (1 if current_question(con, entry) else 0)
            raise ValueError(f"desk's Orch {row['orch_id']} is offline ({health['reason']}); {open_questions} open "
                             "question(s) stay with it. Run `orchd desk --new` to start a new Orch and bind the desk "
                             "to it")
        orch_id, started = start_orch()["id"], True
        bind(con, rt, orch_id, entry_id, force=True)
    trusted = codex_trusted(home)
    return dict(desk=str(home), orch_id=orch_id, orch_model=store.get_orch(con, orch_id)["model"],
                started_new_orch=started, orch_health=reachability(con, rt, orch_id), files=files,
                codex_trusted="unknown" if trusted is None else trusted,
                next=("Open the desk folder in the Desktop app (permissions: Custom (config.toml)) and start talking."
                      if trusted else f"Trust {home} in Codex once, then open it in the Desktop app "
                                       "(permissions: Custom (config.toml))."))
