"""Line-delimited JSON-RPC MCP server for the Orch session (stdio).

The caller's id is `ORCHD_ORCH_ID` from the environment when set (a Claude Orch started by
`orchd orch`; Claude Code sends no thread id). Otherwise it is `params._meta.threadId`, which both
codex exec and the Desktop app send on every tools/call (verified 2026-09-29); such a Codex thread is
registered as a codex Orch on its first call other than the read-only `list_orchs`.

`orchd mcp --role entry` serves the Desktop entry (issue #37) instead: only ENTRY_TOOLS are listed or callable,
their arguments are ids (never a body), and the caller is never registered as an Orch. This guards orchd's
own tools only; the Codex host's other tools are limited by the entry project's config, not here.
"""
import json
import os
import sys
import traceback

from . import core, entry, inventory, store, verify as verification
from .runtime import DEFAULT_WORKER_MODEL, WORKER_MODELS, Runtime

TOOLS = [
    {"name": "list_orchs",
     "annotations": {"readOnlyHint": True},
     "description": "Read-only registry inventory, including Orchs with no open tasks. Returns orch_id, kind, "
                    "created_at, stopped_at, health (alive/dead/unknown), health_reason and open_task_count; "
                    "counts covers registered Orchs only. stopped_at is bookkeeping, not liveness. "
                    "Codex health is unknown. unidentified_claude_sessions lists live background sessions "
                    "excluding registered Orchs and known workers; their role is unknown, null if unavailable. "
                    "Does not register the caller, stop sessions or clean up records.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "dispatch",
     "description": "Start one worker (Claude or Codex, by model) for one task in a fresh worktree of a repo under ~/projects. "
                    "Returns immediately; the worker reports later and you are woken with an [orchd] message. "
                    "The result lists other_open_on_repo: open tasks on the same repo from other Orchs.",
     "inputSchema": {"type": "object", "required": ["repo", "title", "instructions", "done_when", "model_reason",
                                                   "task_type"], "properties": {
         "repo": {"type": "string", "description": "Directory name under ~/projects, e.g. vpin-hub"},
         "title": {"type": "string"},
         "instructions": {"type": "string", "description": "Goal, scope, allowed actions, what to report"},
         "done_when": {"type": "string", "description": "Checkable completion conditions"},
         "model": {"type": "string", "enum": list(WORKER_MODELS), "default": DEFAULT_WORKER_MODEL,
                   "description": "sol = GPT-6.1 Sol on Codex, the default and preferred worker; "
                                  "sonnet = Claude Sonnet 5.5, use when switching to another vendor"},
         "model_reason": {"type": "string", "description": "One sentence: why this model for this task"},
         "task_type": {"type": "string", "enum": list(core.TASK_TYPES)},
         "rework_of": {"type": "string", "description": "Task id this task redoes or fixes"},
         "found_by": {"type": "string", "enum": list(core.FOUND_BY),
                      "description": "Who found the problem; only with rework_of"},
         "verify": {"type": "string", "description": "Runnable command that proves done_when; lock_verify locks it"},
         "manual_checks": {"type": "string", "description": "Only what truly cannot be scripted"},
         "verifies": {"type": "string", "description":
                      "Author task id this worker independently verifies: it runs `orchd verify <its own task id>`, "
                      "which reruns the author's locked command at the locked SHA"}}}},
    {"name": "lock_verify",
     "description": "Lock a task's verify command after its author reports: orchd records the author's current HEAD, "
                    "the git object hash of each path (the test/verify files) at that HEAD, and the command. Refused "
                    "if the worktree is dirty or a path is absolute, has '..' or .git, is untracked, or goes through "
                    "a symlink. Re-locking returns changed_paths against the previous lock. Then dispatch a different "
                    "worker with verifies=<task_id>. The author's own test runs never count as acceptance; inbox "
                    "verification.state is pass only after that independent rerun passed at the locked SHA and the "
                    "author has made no newer commit (otherwise stale). Same local user: tamper-evident, not "
                    "tamperproof, so also check the verifier's own report.",
     "inputSchema": {"type": "object", "required": ["task_id", "paths"], "properties": {
         "task_id": {"type": "string"},
         "paths": {"type": "array", "items": {"type": "string"}, "description": "Repo-relative files or dirs to hash"},
         "command": {"type": "string", "description": "Defaults to the task's dispatch verify"}}}},
    {"name": "inbox",
     "description": "Read unread acks, progress, reports and questions for tasks you dispatched; each message carries the task's "
                    "stored full model id in `model` (\"unknown\" for tasks dispatched before models were stored). "
                    "pending is the task's count of undelivered queued answers and followups, even if closed; "
                    "it reveals no queued body and does not consume or flush the queue. "
                    "Call it when an [orchd] message arrives.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_open",
     "description": "List every task that is not closed, across all Orch sessions, with worker liveness. "
                    "model is the task's stored full model id (\"unknown\" if none was stored). "
                    "pending counts undelivered queued answers and followups without revealing their bodies, "
                    "consuming them or flushing; it remains nonzero until delivery is recorded, including "
                    "during a flush or after a failed receipt. "
                    "worker_alive null: unknown, or a Codex worker between turns (it asked or reported and can "
                    "still be answered); false: its process ended without a report. "
                    "owner_health is independent: alive/dead from a successful Claude job probe, unknown "
                    "when unverified (including Codex owners). notification_delivery shows unread counts "
                    "and safe wake-failure metadata without consuming inbox or revealing message content. "
                    "This does not adopt tasks or change their owner.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "answer",
     "description": "Send an answer to a worker's question. For outward sends, pass Nat's decision verbatim. "
                    "Returns status delivered|queued|failed with delivered and pending counts. A Codex worker gets "
                    "answers as a new turn on its thread; if its turn is still running this waits up to 60s, then "
                    "queues the answer (status queued: stored, NOT yet seen by the worker). Nothing sends queued "
                    "answers by itself: whenever that worker next reports, asks or sends progress and you read the "
                    "inbox, call answer with flush=true (or with a new text) to send every pending answer, oldest "
                    "first, in one turn. If you get queued while the task is in status question, the worker already asked "
                    "and is only finishing its exit, so no further wake will come: retry flush=true after a short "
                    "wait, or once list_open shows worker_alive null. failed: the turn could not start; the answers "
                    "stay queued, retry with "
                    "flush=true instead of resending the text. failed with uncertain=true: a turn did start but its "
                    "receipt could not be written, so it was stopped (or the error says MAY STILL BE RUNNING). The "
                    "answers already reached that worker but still read as pending, so a later flush sends them "
                    "again: check its log or wait for its report first. flush=true with no text also just shows pending.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"}, "text": {"type": "string", "description": "The answer; omit only with flush"},
         "flush": {"type": "boolean", "description": "Send queued answers to an idle Codex worker"},
         "entry_reply_id": {"type": "integer", "description":
                            "Instead of text: send Nat's reply from entry_inbox verbatim from orchd's store. Refused "
                            "if that reply answers a question of another task"}}}},
    {"name": "close",
     "description": "Close a task: stop its worker, remove its worktree if clean and pushed, otherwise keep it and say why.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"},
         "outcome": {"type": "string", "enum": list(core.OUTCOMES)},
         "rating": {"type": "integer", "minimum": 1, "maximum": 3, "description": "Nat's optional 1-3 score"}}}},
    {"name": "view_worker",
     "description": "Open a Ghostty window attached to a task's worker so Nat can watch or type. A Codex worker opens only "
                    "between turns, as `codex resume`; do not answer it while Nat is typing there.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}}},
    {"name": "retry",
     "description": "Replace a task's worker with a new one on the same worktree and branch, keeping all its local work "
                    "(uncommitted, untracked, stash, commits). Use sol (the default and preferred worker) or "
                    "sonnet when switching to another vendor; the from -> to change and your reason are recorded. Stops only "
                    "that task's worker and starts nothing if it does not stop. Refused for closed tasks or a "
                    "missing worktree. The new worker gets the task, its latest progress/report and your reason.",
     "inputSchema": {"type": "object", "required": ["task_id", "model", "reason"], "properties": {
         "task_id": {"type": "string"},
         "model": {"type": "string", "enum": list(WORKER_MODELS),
                   "description": "sol = GPT-6.1 Sol on Codex, the default and preferred worker; "
                                  "sonnet = Claude Sonnet 5.5, use when switching to another vendor"},
         "reason": {"type": "string", "description": "Why this worker is being replaced and why this model"}}}},
    {"name": "followup",
     "description": "Add an instruction to an open task: the same worker, worktree, branch and session continue; the model "
                    "never changes (use retry for that) and no new task or owner is created. Refused for a closed or "
                    "unknown task. It uses the answer delivery path: a Claude worker is sent it at once under the task "
                    "lock; a Codex worker mid-turn queues it in the same FIFO as answers (status queued; flush with "
                    "answer flush=true after its next progress/ask/report; if the task is in question status the worker has already asked and will not wake you again, so flush later yourself, or when list_open worker_alive turns null, same as for answer). Returns status delivered|queued|failed "
                    "with delivered and pending counts, and errors if it could not be sent (nothing is lost if "
                    "queued). A delivered result with record_error was sent; only orchd's bookkeeping failed, so do "
                    "not resend it. If the task lock stays busy (close, retry or adopt running) it raises and nothing "
                    "is accepted: call again later. The worker's earlier report and events are kept; it acks, may send progress, and ends "
                    "with a new report, which wakes you as usual.",
     "inputSchema": {"type": "object", "required": ["task_id", "message"], "properties": {
         "task_id": {"type": "string"},
         "message": {"type": "string", "description": "The next instruction, with any new scope or done_when"}}}},
]

# Orch-side tools for the Desktop entry; refused unless the caller is the Orch the entry is bound to.
TOOLS += [
    {"name": "entry_inbox",
     "description": "Read Nat's unread messages from the Desktop entry, verbatim from the entry thread's saved history, "
                    "with body_bytes/body_sha256. A reply carries reply_to (your question id), task_id and "
                    "worker_question_message_id: forward it to the worker with answer(task_id, entry_reply_id=<message_id>) "
                    "so the text is not retyped. Also returns current_question and queued_questions. "
                    "Call it when an [orchd entry] message arrives.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "send_to_nat",
     "description": "Send a message to Nat's Desktop entry thread; it is queued there byte for byte. Returns delivery "
                    "pending|delivered|failed|uncertain: delivered means it reached the Desktop queue, not that Nat "
                    "read or approved it. failed rows are retried on the entry's next call; uncertain ones are not.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}},
    {"name": "ask_nat",
     "description": "Ask Nat one question through the Desktop entry. Only one question is open at a time: if one is "
                    "already open this one is queued (state queued) and sent after Nat answers the earlier ones, in "
                    "order. Pass task_id when it is about a task; quote_worker_question=true appends that task's "
                    "latest worker question verbatim from orchd's store (use it for outward-send previews instead "
                    "of retyping them). Nat's answer arrives in entry_inbox as kind reply with reply_to=question_id.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Your question; optional with quote_worker_question"},
         "task_id": {"type": "string"},
         "quote_worker_question": {"type": "boolean"}}}},
]

ENTRY_TOOLS = [
    {"name": "relay",
     "description": "Pass Nat's latest message in this thread to the bound Orch. orchd reads the text itself from this "
                    "thread's saved history; never retype, shorten or summarize it. Pass reply_to only when Nat's "
                    "message answers the open [orchd question N]. Returns ids, body_bytes, body_sha256 and status: "
                    "delivered (reached the Orch, not yet read), not_delivered (the Orch is offline; kept), failed or "
                    "uncertain (kept, retried on the next call), duplicate, or source_not_ready (call again).",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "reply_to": {"type": "integer", "description": "The open question's id, only if this message answers it"}}}},
    {"name": "status",
     "description": "Binding and Orch health, the open question in full, how many are queued, Nat messages not yet "
                    "delivered, and Orch messages that have not reached this thread yet (with their full text). "
                    "Call it when the conversation starts or reopens. Show wire_text exactly as returned.",
     "annotations": {"readOnlyHint": False},
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
]

ENTRY_INSTRUCTIONS = (
    "You are Nat's Desktop entry to one orchd Orch. You only pass messages. Call status when the conversation "
    "starts or reopens. When Nat writes, call relay (with reply_to=N only if it answers the open [orchd question N]); "
    "orchd reads Nat's text from the thread itself, so never retype it. Messages from the Orch arrive as "
    "[orchd message N] / [orchd question N]: every time one arrives, say its body (below the header) once, word for "
    "word, in text and in voice mode alike; never shorten, summarize, rephrase or add anything. Do not classify, schedule, decide "
    "for Nat, or start work. What Nat approves is the original on screen, not what you read aloud. Say nothing around "
    "relay when its status is delivered or duplicate; only for not_delivered, failed, uncertain or source_not_ready "
    "tell Nat in one sentence that it did not get through and why. delivered never means read or approved.")


def call(name, args, thread, con, rt):
    if name == "dispatch":
        t = core.dispatch(con, rt, orch_thread=thread, repo=args["repo"], title=args["title"],
                          instructions=args["instructions"], done_when=args["done_when"],
                          model=args.get("model") or DEFAULT_WORKER_MODEL, model_reason=args.get("model_reason"),
                          task_type=args.get("task_type"), rework_of=args.get("rework_of"),
                          found_by=args.get("found_by"), verify=args.get("verify"),
                          manual_checks=args.get("manual_checks"), verifies=args.get("verifies"))
        return {"task_id": t["id"], "status": t["status"], "branch": t["branch"], "worktree": t["worktree"],
                "orch_id": thread, "model": t["model"],
                "other_open_on_repo": core.other_open_on_repo(con, t["repo"], thread, t["id"])}
    if name == "inbox":
        if not thread:
            raise ValueError("inbox needs the caller's thread id")
        return core.inbox(con, thread)
    if name == "list_orchs":
        return inventory.list_orchs(con, rt)
    if name == "list_open":
        return core.list_open(con, rt)
    if name == "answer":
        text = args.get("text")
        if args.get("entry_reply_id") is not None:
            if text is not None:
                raise ValueError("pass text or entry_reply_id, not both")
            text = entry.reply_text(con, thread, args["entry_reply_id"], args["task_id"])
        return core.answer(con, rt, args["task_id"], text, flush=bool(args.get("flush")))
    if name == "entry_inbox":
        return entry.inbox(con, thread)
    if name == "send_to_nat":
        return entry.send_to_nat(con, rt, thread, args.get("text"))
    if name == "ask_nat":
        return entry.ask_nat(con, rt, thread, args.get("text"), args.get("task_id"),
                             bool(args.get("quote_worker_question")))
    if name == "lock_verify":
        return verification.lock(con, args["task_id"], args.get("paths"), args.get("command"), orch_thread=thread)
    if name == "close":
        return core.close(con, rt, args["task_id"], args.get("outcome"), args.get("rating"))
    if name == "view_worker":
        return core.view(con, rt, args["task_id"])
    if name == "retry":
        t = core.retry(con, rt, args["task_id"], args.get("model"), args.get("reason"))
        return {"task_id": t["id"], "status": t["status"], "model": t["model"], "branch": t["branch"],
                "worktree": t["worktree"]}
    if name == "followup":
        return core.followup(con, rt, args["task_id"], args.get("message"))
    raise ValueError(f"unknown tool {name}")


def entry_call(name, args, meta, con, rt, entry_id):
    tool = next((t for t in ENTRY_TOOLS if t["name"] == name), None)
    if tool is None:
        raise PermissionError(f"{name} is not available to the Desktop entry")
    extra = set(args) - set(tool["inputSchema"]["properties"])
    if extra:
        raise PermissionError(f"unexpected argument(s) {', '.join(sorted(extra))}; the entry passes ids only")
    turn = meta.get("x-codex-turn-metadata") or {}
    thread = meta.get("threadId") or turn.get("thread_id")
    if name == "relay":
        return entry.relay(con, rt, entry_id, thread, turn_id=turn.get("turn_id"), reply_to=args.get("reply_to"))
    return entry.status(con, rt, entry_id, thread)


def handle(msg, con, rt, role="orch", entry_id=entry.DEFAULT_ENTRY):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "orchd", "version": "0.1"}}
        if role == "entry":
            result["instructions"] = ENTRY_INSTRUCTIONS
    elif method == "tools/list":
        result = {"tools": ENTRY_TOOLS if role == "entry" else TOOLS}
    elif method == "tools/call":
        meta = params.get("_meta") or {}
        try:
            if role == "entry":  # never an Orch: no ORCHD_ORCH_ID, no registration
                print(f"orchd entry meta keys: {sorted(meta)}", file=sys.stderr)
                data = entry_call(params.get("name"), params.get("arguments") or {}, meta, con, rt, entry_id)
            else:
                thread = os.environ.get("ORCHD_ORCH_ID")
                if not thread:
                    thread = meta.get("threadId") or (meta.get("x-codex-turn-metadata") or {}).get("thread_id")
                    if thread and params.get("name") != "list_orchs":
                        store.register_orch(con, thread, "codex")
                data = call(params.get("name"), params.get("arguments") or {}, thread, con, rt)
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}]}
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            result = {"isError": True, "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout, con=None, rt=None, role="orch", entry_id=entry.DEFAULT_ENTRY):
    con = con or store.connect()
    rt = rt or Runtime()
    for line in stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), con, rt, role, entry_id)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
