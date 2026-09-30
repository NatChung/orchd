"""Line-delimited JSON-RPC MCP server for the Orch session (stdio).

The caller's id is `ORCHD_ORCH_ID` from the environment when set (a Claude Orch started by
`orchd orch`; Claude Code sends no thread id). Otherwise it is `params._meta.threadId`, which both
codex exec and the Desktop app send on every tools/call (verified 2026-09-29); such a Codex thread is
registered as a codex Orch on its first call.
"""
import json
import os
import sys
import traceback

from . import core, store
from .runtime import DEFAULT_WORKER_MODEL, Runtime

TOOLS = [
    {"name": "dispatch",
     "description": "Start one Claude worker for one task in a fresh worktree of a repo under ~/projects. "
                    "Returns immediately; the worker reports later and you are woken with an [orchd] message. "
                    "The result lists other_open_on_repo: open tasks on the same repo from other Orchs.",
     "inputSchema": {"type": "object", "required": ["repo", "title", "instructions", "done_when", "model_reason",
                                                   "task_type"], "properties": {
         "repo": {"type": "string", "description": "Directory name under ~/projects, e.g. vpin-hub"},
         "title": {"type": "string"},
         "instructions": {"type": "string", "description": "Goal, scope, allowed actions, what to report"},
         "done_when": {"type": "string", "description": "Checkable completion conditions"},
         "model": {"type": "string", "enum": ["sonnet", "opus"], "description":
                   "sonnet = default for clear-scope implementation and writing verify scripts; opus = ambiguous "
                   "requirements, cross-repo, outward communication, review, or after sonnet failed twice"},
         "model_reason": {"type": "string", "description": "One sentence: why this model for this task"},
         "task_type": {"type": "string", "enum": list(core.TASK_TYPES)},
         "rework_of": {"type": "string", "description": "Task id this task redoes or fixes"},
         "found_by": {"type": "string", "enum": list(core.FOUND_BY),
                      "description": "Who found the problem; only with rework_of"}}}},
    {"name": "inbox",
     "description": "Read unread acks, reports and questions for tasks you dispatched. Call it when an [orchd] message arrives.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_open",
     "description": "List every task that is not closed, across all Orch sessions, with worker liveness.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "answer",
     "description": "Send an answer to a worker's question. For outward sends, pass Nat's decision verbatim.",
     "inputSchema": {"type": "object", "required": ["task_id", "text"], "properties": {
         "task_id": {"type": "string"}, "text": {"type": "string"}}}},
    {"name": "close",
     "description": "Close a task: stop its worker, remove its worktree if clean and pushed, otherwise keep it and say why.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"},
         "outcome": {"type": "string", "enum": list(core.OUTCOMES)},
         "rating": {"type": "integer", "minimum": 1, "maximum": 3, "description": "Nat's optional 1-3 score"}}}},
    {"name": "view_worker",
     "description": "Open a Ghostty window attached to a task's worker so Nat can watch or type.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}}},
]


def call(name, args, thread, con, rt):
    if name == "dispatch":
        t = core.dispatch(con, rt, orch_thread=thread, repo=args["repo"], title=args["title"],
                          instructions=args["instructions"], done_when=args["done_when"],
                          model=args.get("model") or DEFAULT_WORKER_MODEL, model_reason=args.get("model_reason"),
                          task_type=args.get("task_type"), rework_of=args.get("rework_of"),
                          found_by=args.get("found_by"))
        return {"task_id": t["id"], "status": t["status"], "branch": t["branch"], "worktree": t["worktree"],
                "orch_id": thread, "model": t["model"],
                "other_open_on_repo": core.other_open_on_repo(con, t["repo"], thread, t["id"])}
    if name == "inbox":
        if not thread:
            raise ValueError("inbox needs the caller's thread id")
        return core.inbox(con, thread)
    if name == "list_open":
        return core.list_open(con, rt)
    if name == "answer":
        core.answer(con, rt, args["task_id"], args["text"])
        return {"sent": True}
    if name == "close":
        return core.close(con, rt, args["task_id"], args.get("outcome"), args.get("rating"))
    if name == "view_worker":
        return core.view(con, rt, args["task_id"])
    raise ValueError(f"unknown tool {name}")


def handle(msg, con, rt):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "orchd", "version": "0.1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        meta = params.get("_meta") or {}
        thread = os.environ.get("ORCHD_ORCH_ID")
        if not thread:
            thread = meta.get("threadId") or (meta.get("x-codex-turn-metadata") or {}).get("thread_id")
            if thread:
                store.register_orch(con, thread, "codex")
        try:
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


def serve(stdin=sys.stdin, stdout=sys.stdout, con=None, rt=None):
    con = con or store.connect()
    rt = rt or Runtime()
    for line in stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), con, rt)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
