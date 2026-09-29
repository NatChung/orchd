"""Line-delimited JSON-RPC MCP server for the Orch session (stdio).

The caller's thread id comes from `params._meta.threadId`, which both codex exec
and the Desktop app send on every tools/call (verified 2026-09-29).
"""
import json
import sys
import traceback

from . import core, store
from .runtime import Runtime

TOOLS = [
    {"name": "dispatch",
     "description": "Start one worker (Claude Opus) for one task in a fresh worktree of a repo under ~/projects. "
                    "Returns immediately; the worker reports later and you are woken with an [orchd] message.",
     "inputSchema": {"type": "object", "required": ["repo", "title", "instructions", "done_when"], "properties": {
         "repo": {"type": "string", "description": "Directory name under ~/projects, e.g. vpin-hub"},
         "title": {"type": "string"},
         "instructions": {"type": "string", "description": "Goal, scope, allowed actions, what to report"},
         "done_when": {"type": "string", "description": "Checkable completion conditions"}}}},
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
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}}},
    {"name": "view_worker",
     "description": "Open a Ghostty window attached to a task's worker so Nat can watch or type.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}}},
]


def call(name, args, thread, con, rt):
    if name == "dispatch":
        t = core.dispatch(con, rt, orch_thread=thread, repo=args["repo"], title=args["title"],
                          instructions=args["instructions"], done_when=args["done_when"])
        return {"task_id": t["id"], "status": t["status"], "branch": t["branch"], "worktree": t["worktree"]}
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
        return core.close(con, rt, args["task_id"])
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
        thread = meta.get("threadId") or (meta.get("x-codex-turn-metadata") or {}).get("thread_id")
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
