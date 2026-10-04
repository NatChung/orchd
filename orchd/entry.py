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
import os
import re
import sqlite3
import time
from pathlib import Path

from . import orch_revive, paths, store
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
        raise ValueError(f"entry {entry_id} is not bound to an Orch; Nat binds it with `orchd binding`")
    return row


def reachability(con, rt, orch_id):
    """Q1: a stopped or confirmed-dead Orch is offline; nothing here ever starts or replaces one.
    Delivery revives a retired one in place (orch_revive, #72); this read never does."""
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
            raise ValueError(f"entry {entry_id} is bound to {row['orch_id']}; rebinding it needs force (`orchd binding --to`)")
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
    if row["source_raw"]:
        what = "voice " + what
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
        orch_id = entry["orch_id"]
        health = reachability(con, rt, orch_id)
        if health["state"] == "dead":
            error = f"orch offline: {health['reason']}"
            seen = orch_revive.retired(rt, store.get_orch(con, orch_id), health)
            if seen:
                try:  # retired by Claude's daemon, not stopped: resume the same Orch (#72)
                    orch_revive.revive(con, rt, orch_id, seen_job=seen)
                    error = None
                except Exception as revive_error:
                    error += f"; revive failed: {type(revive_error).__name__}: {revive_error}"[:400]
            if error:
                con.executemany("UPDATE entry_messages SET delivery_error=? WHERE id=?",
                                [(error, r["id"]) for r in rows])
                return {r["id"]: "not_delivered" for r in rows}
        return _send(con, rows, orch_id, lambda row: orch_revive.send(con, rt, orch_id, inbound_wake(row)))


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


VOICE_LABEL = "[語音輸入，可能有辨識錯字]"


def _tag(name, text):
    found = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
    return found.group(1) if found else None


def _voice_parts(text):
    """(source, request, heard) of a <realtime_delegation> block; heard is the user lines after the last
    assistant line, i.e. what Nat said in this round."""
    source = (_tag("source", text) or "").strip()
    request = (_tag("input", text) or "").strip()
    said, role = [], None
    for line in (_tag("transcript_delta", text) or "").splitlines():
        if line.startswith("assistant:"):
            role, said = "assistant", []
        elif line.startswith("user:"):
            role = "user"
            said.append(line[len("user:"):].strip())
        elif role == "user" and said:
            said[-1] += "\n" + line
    return source, request, "\n".join(s for s in said if s)


def _after(text, earlier):
    """What `text` adds to `earlier` when it only grows it (voice mode re-sends the growing sentence)."""
    if earlier and text.startswith(earlier):
        return text[len(earlier):].lstrip(" ,，。.、!！?？")
    return text


VOICE_RECENT = 600  # seconds: transcript lines relayed this recently are not relayed again


def voice_input(text, earlier=None, already=()):
    """None for typed text. Desktop voice mode hands the interface a <realtime_delegation> block: the voice
    model's request in <input>, plus a transcript that repeats earlier rounds. Returns {"handoff": True} for the
    end-of-session transcript flush (not Nat asking for anything); {"repeat": True} when, against `earlier` (the
    raw block already relayed in the same turn, as the voice model re-sends a sentence while Nat is still
    talking), nothing new was said; else {"text": what the Orch should get, "continues": bool}. `already` holds
    transcript lines relayed recently in this thread: the voice model can also split one utterance across turns
    and repeat the earlier part in the next transcript, so those exact lines are dropped."""
    if not text.lstrip().startswith("<realtime_delegation>"):
        return None
    source, request, heard = _voice_parts(text)
    if source == "transcript_tail_flush":
        return {"handoff": True}
    continues = False
    if earlier:
        _, old_request, old_heard = _voice_parts(earlier)
        new_request, new_heard = _after(request, old_request), _after(heard, old_heard)
        continues = (new_request, new_heard) != (request, heard)
        if continues and not new_request and not new_heard:
            return {"repeat": True}
        request, heard = new_request, new_heard
    if already and heard:
        kept = [line for line in heard.split("\n") if line.strip() and line.strip() not in already]
        if not kept and not request:
            return {"repeat": True}
        heard = "\n".join(kept)
    if not request and not heard:
        return {"text": text, "continues": False}
    parts = [VOICE_LABEL + ("（接續上一則）" if continues else ""), request or heard]
    if request and heard and heard != request:
        parts.append(f"（語音逐字稿：{heard}）")
    return {"text": "\n".join(parts), "continues": continues}


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
    earlier = None
    if source.get("turn_id"):  # the voice model re-sends a growing sentence within one turn
        row = con.execute("SELECT source_raw FROM entry_messages WHERE source_thread=? AND source_turn=? "
                          "AND source_raw IS NOT NULL AND kind IN ('message','reply') ORDER BY id DESC LIMIT 1",
                          (thread, source["turn_id"])).fetchone()
        earlier = row["source_raw"] if row else None
    already = set()
    for row in con.execute("SELECT source_raw FROM entry_messages WHERE source_thread=? AND source_raw IS NOT NULL "
                           "AND kind IN ('message','reply') AND created_at>?", (thread, time.time() - VOICE_RECENT)):
        already.update(line.strip() for line in _voice_parts(row["source_raw"])[2].split("\n") if line.strip())
    voice = voice_input(source["text"], earlier, already)
    if voice and (voice.get("handoff") or voice.get("repeat")):  # kept as a record, never delivered or read
        kind = "handoff" if voice.get("handoff") else "voice_repeat"
        try:
            with store.immediate(con):
                message_id = _insert(con, get_entry(con, entry_id), "in", kind, source["text"],
                                     delivery="skipped", read_at=time.time(), source_raw=source["text"],
                                     source_turn=source.get("turn_id"), source_thread=thread,
                                     source_item_id=source["item_id"])
        except sqlite3.IntegrityError:
            return dict(status="duplicate", resumed=resumed)
        if kind == "handoff":
            return dict(message_id=message_id, kind=kind, status="skipped_handoff", resumed=resumed,
                        note="Desktop's end-of-voice-session handoff, not a request from Nat; not passed to the "
                             "Orch. Say nothing.")
        return dict(message_id=message_id, kind=kind, status="duplicate", resumed=resumed,
                    note="the voice model re-sent what was already relayed in this turn; nothing new to pass on")
    body = voice["text"] if voice else source["text"]
    note = None
    try:
        with store.immediate(con):
            entry = get_entry(con, entry_id)
            fields = dict(source_thread=thread, source_item_id=source["item_id"],
                          source_raw=source["text"] if voice else None,
                          source_turn=source.get("turn_id") if voice else None)
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
                message_id = _insert(con, entry, "in", "reply", body, **fields)
                following = queued_questions(con, entry)
                if following:
                    _make_current(con, entry, following[0])
            else:
                message_id = _insert(con, entry, "in", "message", body, **fields)
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
                          "automatically, check `orchd binding --status`")
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


# -- binding: the Desktop entry folder (`orchd init` creates it, `orchd binding` binds it) --------------------

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


def binding(con, rt, start_orch, new=False, entry_id=DEFAULT_ENTRY):
    """Operator: make sure the interface is bound to a live Claude Orch.

    Reuses the bound Orch unless it is confirmed dead (Q3). One Claude's daemon retired for idling is resumed in
    place (#72). A stopped one is never replaced silently (Q1): that needs new=True, which starts a new Orch and
    rebinds; the old Orch's open questions stay with it. With no binding yet, it starts one Orch and binds it.
    The folder itself comes from `orchd init`.
    """
    home = paths.interface_home()
    if not (home / ".codex" / "config.toml").exists():
        raise ValueError(f"{home} is not set up; run `orchd init` first")
    row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    health = reachability(con, rt, row["orch_id"]) if row is not None else None
    revived = False
    seen = orch_revive.retired(rt, store.get_orch(con, row["orch_id"]), health) if row is not None and not new else None
    if seen:
        try:
            orch_revive.revive(con, rt, row["orch_id"], seen_job=seen)
        except Exception as error:
            raise ValueError(f"the interface's Orch {row['orch_id']} was retired by Claude and resuming it failed "
                             f"({type(error).__name__}: {error}). Run `orchd binding --new` to start a new Orch and "
                             "bind the interface to it") from error
        health, revived = reachability(con, rt, row["orch_id"]), True
    if row is not None and health["state"] != "dead" and not new:
        orch_id, started = row["orch_id"], False
    else:
        if row is not None and not new:
            entry = get_entry(con, entry_id)
            open_questions = len(queued_questions(con, entry)) + (1 if current_question(con, entry) else 0)
            raise ValueError(f"the interface's Orch {row['orch_id']} is offline ({health['reason']}); {open_questions} "
                             "open question(s) stay with it. Run `orchd binding --new` to start a new Orch and "
                             "bind the interface to it")
        orch_id, started = start_orch()["id"], True
        bind(con, rt, orch_id, entry_id, force=True)
    trusted = codex_trusted(home)
    return dict(interface=str(home), orch_id=orch_id, orch_model=store.get_orch(con, orch_id)["model"],
                started_new_orch=started, revived_orch=revived, orch_health=reachability(con, rt, orch_id),
                codex_trusted="unknown" if trusted is None else trusted,
                next=(f"Open {home} in the Codex Desktop app (permissions: interface) and start talking."
                      if trusted else f"Run `orchd init` to trust {home} in Codex, then open it in the Desktop app."))
