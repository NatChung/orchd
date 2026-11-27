#!/usr/bin/env python3
"""Check the receipts from the manual macOS Desktop test. See docs/desktop-manual-e2e.md."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from orchd.runtime import Runtime


def one(rows, label):
    if len(rows) != 1:
        raise ValueError(f"{label}: expected one receipt, got {len(rows)}")
    return rows[0]


def read_case(con, label, phase):
    prefix = f"DESKTOP_E2E_{label}_"
    dispatch = one(con.execute(
        "SELECT * FROM entry_messages WHERE direction='in' AND kind='message' AND instr(body,?)>0",
        (prefix + "DISPATCH",)).fetchall(), f"{label} dispatch")
    binding = one(con.execute("SELECT * FROM entries WHERE id=?", (dispatch["entry_id"],)).fetchall(),
                  f"{label} binding")
    question = one(con.execute(
        "SELECT * FROM entry_messages WHERE kind='question' AND direction='out' AND instr(body,?)>0",
        (prefix + "PICK",)).fetchall(), f"{label} question")
    task = one(con.execute("SELECT * FROM tasks WHERE id=?", (question["task_id"],)).fetchall(),
               f"{label} task")
    worker_question = one(con.execute(
        "SELECT * FROM messages WHERE id=? AND kind='question' AND task_id=? AND instr(body,?)>0",
        (question["source_message_id"], task["id"], prefix + "PICK")).fetchall(),
        f"{label} worker question")
    question_state = "answered" if phase == "answered" else "current"
    if not (binding["id"] == "chat-" + binding["thread_id"] == question["entry_id"]
            and binding["thread_id"] == dispatch["source_thread"]
            and binding["orch_id"] == dispatch["orch_id"] == question["orch_id"] == task["orch_thread"]
            and dispatch["delivery"] == question["delivery"] == "delivered"
            and dispatch["read_at"] is not None and question["question_state"] == question_state
            and worker_question["body"] in question["body"]
            and task["backend"] == "app-server"):
        raise ValueError(f"{label}: dispatch, binding, worker question or task does not match")

    result = dict(thread_id=binding["thread_id"], orch_id=binding["orch_id"], bound_at=binding["bound_at"],
                  seen_at=binding["thread_seen_at"], question_id=question["id"], task_id=task["id"])
    if phase != "answered":
        return result

    reply = one(con.execute("SELECT * FROM entry_messages WHERE kind='reply' AND reply_to=?",
                            (question["id"],)).fetchall(), f"{label} reply")
    answer = one(con.execute("SELECT * FROM messages WHERE kind='answer' AND entry_reply_id=?",
                             (reply["id"],)).fetchall(), f"{label} answer")
    expected = f'回答 #{question["id"]}：選 {label}。{prefix}SELECTED {label}'
    source = one([item for item in Runtime().codex_user_messages(binding["thread_id"])
                  if item["item_id"] == reply["source_item_id"]], f"{label} Desktop input")
    report = con.execute(
        "SELECT id FROM messages WHERE task_id=? AND kind='report' "
        "AND (instr(body,?)>0 OR instr(coalesce(evidence,''),?)>0) ORDER BY id DESC LIMIT 1",
        (task["id"], expected, expected)).fetchone()
    close = con.execute("SELECT body FROM messages WHERE task_id=? AND kind='close' ORDER BY id DESC LIMIT 1",
                        (task["id"],)).fetchone()
    if not (reply["entry_id"] == binding["id"] and reply["source_thread"] == binding["thread_id"]
            and reply["orch_id"] == binding["orch_id"] and reply["task_id"] == task["id"] == answer["task_id"]
            and reply["delivery"] == "delivered" and reply["read_at"] is not None
            and source["text"] == reply["body"] == answer["body"]
            and answer["body"].rstrip("\n") == expected and report
            and close and json.loads(close["body"])["outcome"] == "done"
            and task["status"] == "closed" and task["worktree"] and not Path(task["worktree"]).exists()):
        raise ValueError(f"{label}: exact reply, worker report or cleanup has not been verified")
    return dict(result, reply_id=reply["id"], answer_id=answer["id"], worker_report_id=report["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("pending", "reopened", "answered"))
    parser.add_argument("root", type=Path, help="temporary root created for this E2E only")
    parser.add_argument("--before", type=Path, help="JSON output of the pending check before reopening")
    args = parser.parse_args()
    if (args.before is None) != (args.phase == "pending"):
        parser.error("provide --before for reopened and answered only")
    db = args.root.resolve() / "state" / "orchd.db"
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN")
            result = {label: read_case(con, label, args.phase) for label in ("A", "B")}
            if con.execute("SELECT count(*) FROM entries").fetchone()[0] != 2 or any(
                    result["A"][key] == result["B"][key]
                    for key in ("thread_id", "orch_id", "question_id", "task_id")):
                raise ValueError("expected exactly two isolated chat, Orch, question and task identities")
        if args.before:
            before = json.loads(args.before.read_text(encoding="utf-8"))
            for label, case in result.items():
                original = before[label]
                if any(case[key] != original[key] for key in (
                        "thread_id", "orch_id", "bound_at", "question_id", "task_id")):
                    raise ValueError(f"{label}: binding or question changed after opening the chat again")
                if case["seen_at"] <= original["seen_at"]:
                    raise ValueError(f"{label}: call status again after reopening the chat")
        print(json.dumps(result, indent=2))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"{args.phase}: {error}\n")


if __name__ == "__main__":
    main()
