"""orchd: Orch MCP server and the worker/operator command line.

  orchd mcp [--role orch|entry] [--entry NAME]   run the MCP server for an Orch session, or for the Desktop entry (stdio)
  orchd orch start [--model opus|sonnet] [--no-attach]   start a Claude Orch, then attach to it
  orchd orch stop ID                          stop a Claude Orch
  orchd orch restart [OLD_ID] [--model opus|sonnet] [--dry-run]
                                              stop, confirm death, start, adopt, and rebind Desktop
  orchd init [--from OLD_ORCH_HOME] [--no-trust]   create Orch, interface and computer-use homes; trust Orch/interface in Codex
  orchd upgrade                               reinstall the uv-installed orchd at its source's newest commit
  orchd entry reconcile [--apply]             move open entry questions stranded on an old owner to the entry's owner (#60)
  orchd binding [--new | --to ORCH_ID | --status] [--entry NAME]
                                              bind ~/orch/interface to a live Claude Orch (starts one if none);
                                              --new starts a new one, --to binds a given one, --status only reads
  orchd ack ID                                worker: acknowledge a task
  orchd report ID --status done|blocked --summary S [--evidence E]
  orchd progress ID "text"                    worker: interim update to the Orch; the task keeps running
  orchd flush ID [--after-pid PID]            run by a Codex turn's shell when codex exits: send answers queued meanwhile
  orchd ask ID "question with full preview"   worker: ask Orch/Operator and wait for an answer
  orchd verify ID [--timeout S]               verifier worker: rerun the verified task's locked command at its locked SHA
  orchd orch list [--all] [--json] [--restore ID]          live/resumable Orchs; --all includes archived/unknown
  orchd orch attach ID [--viewer]                  attach an existing Claude Orch (resume its conversation if idle)
  orchd intake submit|suggest|pending|bind|ack|exclude|classify|deliver|show|decide|resolve|decisions|backlog|reconcile|draft|draft-show|approve|claim|receipt|cancel   local intake queue and reply approvals (docs/intake.md)
  orchd list                                  open tasks, worker/Orch health, unread notification failures
  orchd goal add|set|show|list|export --md      central goals, audit history, markdown snapshot
  orchd board --html PATH                     private static project board, no inbox consumption
  orchd watch [--since HH:MM]                 live timeline of Orch <-> worker messages
  orchd summary [--since HH:MM]               per Orch: workers, models, questions, parallelism, tokens
  orchd stats [--since T] [--json]            per Orch x task_type: counts, rework, source-backed tokens, cost estimate
  orchd config show|get KEY|set KEY VALUE|unset KEY   personal settings (worker-model, projects-dir); show prints each value's source
  orchd doctor [--profile example] [--json] [--no-input]   machine check; first-run terminal setup; exit 1 required fail, 2 required unknown
  orchd adopt NEW_ORCH [TASK_ID...] [--from OLD_ORCH] [--force]   move open tasks to another Orch (operator only)
  orchd close ID                              stop a task's worker and clean its worktree if safe

Orch actions require an explicit subcommand; see `orchd orch --help`.
"""
import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from orchd import core, store
from orchd.runtime import DEFAULT_ORCH_MODEL, ORCH_MODELS, Runtime


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        if self.prog == "orchd":
            replacements = {"orch-stop": "orch stop", "orch-restart": "orch restart",
                            "orchs": "orch list", "attach": "orch attach"}
            for old, new in replacements.items():
                if f"invalid choice: '{old}'" in message:
                    message += f"; `orchd {old}` was removed; use `orchd {new}` instead"
                    break
        elif self.prog == "orchd orch":
            self.print_help(sys.stderr)
            message += "; choose start|stop|restart|list|attach; to start, use `orchd orch start`"
        super().error(message)


def build_parser():
    parser = CommandParser(prog="orchd", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--role", choices=["orch", "entry"], default="orch")
    mcp.add_argument("--entry", default="desktop", help="entry name (with --role entry)")
    interface = sub.add_parser("interface", help="Shared-Orch adapter CLI; JSON stdin/stdout")
    interface_actions = interface.add_subparsers(dest="interface_action", required=True)
    for action in ("register", "submit", "fetch", "ack", "deregister"):
        interface_actions.add_parser(action, help="read a JSON object from stdin")
    interface_actions.add_parser("list", help="read-only interface owners and pending counts; no tokens")
    rebind = interface_actions.add_parser("rebind", help="Operator-only: move all interfaces without stopping either Orch")
    rebind.add_argument("--from", dest="old_id", required=True, metavar="OLD_ORCH_ID")
    rebind.add_argument("--to", dest="new_id", required=True, metavar="NEW_ORCH_ID")
    rebind.add_argument("--dry-run", action="store_true")
    orch = sub.add_parser("orch", help="start, stop, restart, list or attach an Orch")
    def start_options(command):
        command.add_argument("--model", default=DEFAULT_ORCH_MODEL,
                             choices=list(ORCH_MODELS))
        command.add_argument("--no-attach", action="store_true",
                             default=False)

    def restart_options(command):
        command.add_argument("old_id", nargs="?", help="defaults to the Desktop binding's Orch")
        command.add_argument("--model", choices=list(ORCH_MODELS), help="defaults to the old Orch's model")
        command.add_argument("--dry-run", action="store_true", help="read-only plan; no stop, start, adopt or rebind")

    def list_options(command):
        command.add_argument("--json", action="store_true", help="output the complete MCP inventory JSON")
        command.add_argument("--all", action="store_true", help="include dead, unknown and archived Orchs")
        command.add_argument("--restore", metavar="ORCH_ID", help="clear archival and restart the death observation window")

    def attach_options(command):
        command.add_argument("orch_id")
        command.add_argument("--viewer", action="store_true", help="open Ghostty instead of attaching in this terminal")

    actions = orch.add_subparsers(dest="orch_action", required=True)
    start = actions.add_parser("start", help="start a Claude Orch, then attach to it")
    start_options(start)
    stop = actions.add_parser("stop", help="stop a Claude Orch")
    stop.add_argument("orch_id")
    restart = actions.add_parser("restart", help="replace an Orch and adopt its open tasks")
    restart_options(restart)
    listing = actions.add_parser("list", help="list live/resumable Orchs")
    list_options(listing)
    probe = actions.add_parser("register-codex-probe", help="register a managed Codex app-server health probe")
    probe.add_argument("orch_id")
    probe.add_argument("--pid", required=True, type=int, help="app-server PID, never a turn exec PID")
    probe.add_argument("--endpoint", required=True, help="owned app-server socket in a private directory")
    attachment = actions.add_parser("attach", help="attach an existing Claude Orch")
    attach_options(attachment)

    init = sub.add_parser("init")
    init.add_argument("--from", dest="source", help="copy what an old Orch home kept (e.g. ~/projects/orch); never overwrites")
    init.add_argument("--no-trust", action="store_true", help="do not write Codex trust or check Claude trust")
    upgrade_parser = sub.add_parser("upgrade")
    upgrade_parser.add_argument("--migrations-only", action="store_true", help="retry home migrations without reinstalling")
    upgrade_parser.add_argument("--dry-run", action="store_true", help="preview home migrations without writing or reinstalling")
    binding = sub.add_parser("binding")
    binding.add_argument("--entry", default="desktop")
    how = binding.add_mutually_exclusive_group()
    how.add_argument("--new", action="store_true", help="start a new Claude Orch and bind the interface to it")
    how.add_argument("--to", metavar="ORCH_ID", help="bind the interface to this live Claude Orch")
    how.add_argument("--status", action="store_true", help="read-only: binding, Orch health, questions, delivery")
    entry_parser = sub.add_parser("entry", help="entry messages and local-only issue threads")
    entry_actions = entry_parser.add_subparsers(dest="entry_action", required=True)
    thread = entry_actions.add_parser("thread", help="read summaries from a private DB/WAL copy")
    refs = thread.add_mutually_exclusive_group(required=True)
    refs.add_argument("--issue-refs", help="comma-separated refs")
    refs.add_argument("--issue-ref", help="single ref")
    thread.add_argument("--entry", default="desktop")
    thread.add_argument("--limit", type=int, default=3)
    thread.add_argument("--json", action="store_true", help="JSON output (also the default)")
    reconcile = entry_actions.add_parser("reconcile", help="move open questions stranded on a previous owner to the "
                                                       "entry's owner (dry run unless --apply; backs up the DB first)")
    reconcile.add_argument("--apply", action="store_true")
    for action in ("send", "ask", "tag"):
        command = entry_actions.add_parser(action)
        if action == "tag":
            command.add_argument("message_id", type=int)
            tag_ref = command.add_mutually_exclusive_group(required=True)
            tag_ref.add_argument("--issue-ref")
            tag_ref.add_argument("--clear", action="store_true")
        else:
            command.add_argument("--text", required=True)
            command.add_argument("--issue-ref")
            if action == "ask":
                command.add_argument("--task-id")
                command.add_argument("--quote-worker-question", action="store_true")
    questions = sub.add_parser("entry-question", help="explicit question lifecycle and history")
    question_actions = questions.add_subparsers(dest="question_action", required=True)
    for action in ("show", "snooze", "un-snooze", "cancel", "supersede", "answer"):
        command = question_actions.add_parser(action, description="Submit an answer; always prints a JSON receipt." if action == "answer" else None)
        command.add_argument("question_id", type=int)
        command.add_argument("--entry", default="desktop")
        if action == "show":
            continue
        command.add_argument("--expected-version", type=int, required=True)
        command.add_argument("--expected-state", required=True,
                             choices=["current", "queued", "answered", "snoozed", "cancelled", "superseded"])
        if action == "answer":
            command.add_argument("--text", required=True)
            command.add_argument("--orch-id", required=True, help="expected responsible Orch binding")
            command.add_argument("--idempotency-key", required=True)
        elif action == "snooze":
            command.add_argument("--revisit-at", type=float, required=True, help="Unix timestamp; due never auto-promotes")
            command.add_argument("--note")
        elif action != "un-snooze":
            command.add_argument("--reason", required=True)
        if action == "supersede":
            command.add_argument("--text", required=True, help="new question; old answers are not reused")
            command.add_argument("--task-id", help="explicit task link for the new question")
    task = sub.add_parser("task", help="link or unlink a task's parent issue reference")
    task_actions = task.add_subparsers(dest="task_action", required=True)
    for action in ("link", "unlink"):
        command = task_actions.add_parser(action, description="Always prints a JSON receipt; no tracker writes.")
        command.add_argument("--task", required=True)
        if action == "link":
            command.add_argument("--ref", required=True)
        command.add_argument("--actor", choices=("orch", "operator"), default="orch")
        command.add_argument("--idempotency-key", help="Task-scoped durable replay key; generated if omitted")
    task_questions = sub.add_parser("task-question", help="answer the latest pending worker question directly")
    task_question_actions = task_questions.add_subparsers(dest="task_question_action", required=True)
    task_answer = task_question_actions.add_parser("answer", description="Always prints a JSON receipt.")
    task_answer.add_argument("--task", required=True)
    task_answer.add_argument("--question-id", required=True, type=int)
    task_answer.add_argument("--text-file", required=True, type=Path)
    task_answer.add_argument("--idempotency-key", required=True)
    task_answer.add_argument("--actor", choices=("dashboard", "operator"), default="dashboard")
    task_dismiss = task_question_actions.add_parser("dismiss", description="Dismiss a decision; prints a JSON receipt.")
    task_dismiss.add_argument("--task", required=True)
    task_dismiss.add_argument("--question-id", required=True, type=int)
    task_dismiss.add_argument("--reason", required=True)
    task_dismiss.add_argument("--idempotency-key", required=True)
    task_dismiss.add_argument("--actor", choices=("dashboard", "operator"), default="dashboard")
    intake = sub.add_parser("intake", help="local intake queue, reply drafts, approvals and send receipts (JSON receipts)")
    intake_actions = intake.add_subparsers(dest="intake_action", required=True)
    for action, helptext in (("submit", "producer: durable receipt for one normalized event (--file PATH or -)"),
                             ("draft", "create a reply draft or its next version (--file PATH or -)"),
                             ("approve", "approve one exact draft version (--file PATH or -)"),
                             ("receipt", "connector: record sent|failed|uncertain (--file PATH or -)"),
                             ("classify", "annotate an event: classification, level, todo_id, issue_ref, project")):
        command = intake_actions.add_parser(action, help=helptext)
        command.add_argument("--file", default="-", help="JSON input; - is stdin")
        if action == "submit":
            command.add_argument("--producer", required=True, help="name granted in [intake.producers.NAME]")
        if action == "classify":
            command.add_argument("--receipt", required=True)
        if action in ("draft", "approve", "classify"):
            command.add_argument("--actor", choices=("orch", "operator"), default="operator" if action == "approve" else "orch")
        if action == "receipt":
            command.add_argument("--actor", choices=("connector", "operator"), default="connector")
    intake_suggest = intake_actions.add_parser("suggest", help="producer: append a suggestion revision to a v2 event (--file PATH or -)")
    intake_suggest.add_argument("--file", default="-", help="JSON input; - is stdin")
    intake_suggest.add_argument("--producer", required=True, help="name granted in [intake.producers.NAME]")
    intake_suggest.add_argument("--receipt", required=True)
    intake_deliver = intake_actions.add_parser("deliver", help="wake owners with due intake notifications (retry + recovery scan)")
    intake_reconcile = intake_actions.add_parser("notify-reconcile", help="reconcile an uncertain wake after transport read-back")
    intake_reconcile.add_argument("notification_id", type=int)
    intake_reconcile.add_argument("attempt", type=int)
    intake_reconcile.add_argument("--delivered", choices=("yes", "no"), required=True)
    intake_reconcile.add_argument("--detail", required=True)
    intake_deliver.add_argument("--urgent-only", action="store_true", help="only urgent notifications")
    intake_event_show = intake_actions.add_parser("show", help="read-only: one v2 event's original text and suggestions (owner or operator)")
    intake_event_show.add_argument("--receipt", required=True)
    intake_event_show.add_argument("--as", dest="viewer", required=True, help="owning Orch id, or 'operator'")
    for action, helptext in (("decide", "Orch: record attach_existing|create_issue|ask_nat|dispatch|record|ignore for an owned receipt (--file PATH or -)"),
                             ("resolve", "Orch: finish an open decision with read-back evidence (--file PATH or -)")):
        command = intake_actions.add_parser(action, help=helptext)
        command.add_argument("--file", default="-", help="JSON input; - is stdin")
        command.add_argument("--orch", required=True, help="the deciding Orch id (must own the receipt)")
    intake_decisions = intake_actions.add_parser("decisions", help="read-only: decision trace receipt -> issue/goal/task/question")
    intake_decisions.add_argument("--receipt")
    intake_decisions.add_argument("--orch")
    intake_decisions.add_argument("--state", choices=("intent", "uncertain", "completed", "failed"))
    intake_backlog = intake_actions.add_parser("backlog", help="read-only: queues, unassigned, notification failures, open decisions")
    intake_backlog.add_argument("--owner")
    intake_reconcile = intake_actions.add_parser("reconcile", help="move received receipts stranded on a stopped/dead Orch to the "
                                                                 "Orch the Desktop entry is bound to (dry run unless --apply; backs up the DB first)")
    intake_reconcile.add_argument("--apply", action="store_true")
    intake_pending = intake_actions.add_parser("pending", help="unacked events for an owner; does not consume")
    intake_pending.add_argument("--owner", help="Orch id; omit to list unassigned events")
    intake_pending.add_argument("--limit", type=int, default=100)
    intake_pending.add_argument("--after-id", type=int, help="exclusive event id boundary")
    intake_pending.add_argument("--high-water-id", type=int, help="fixed traversal upper bound")
    intake_pending.add_argument("--cursor", help="next_cursor from the preceding page (keeps owner and upper bound)")
    intake_exclude = intake_actions.add_parser("exclude", help="explicit terminal exclusion; no owner required")
    intake_exclude.add_argument("--receipt", required=True, nargs="+", action="extend")
    intake_exclude.add_argument("--reason", required=True)
    intake_exclude.add_argument("--rule-version", required=True)
    intake_exclude.add_argument("--actor", required=True, choices=("orch", "operator"))
    intake_bind = intake_actions.add_parser("bind", help="bind an unassigned event to a registered Orch")
    intake_bind.add_argument("--receipt", required=True)
    intake_bind.add_argument("--owner", required=True)
    intake_bind.add_argument("--only-if-unassigned", action="store_true",
                             help="atomic: bind only if the receipt is received and still has no owner; otherwise report "
                                  "skipped and change nothing (never takes a receipt another Orch already owns)")
    intake_ack = intake_actions.add_parser("ack", help="owner durably took responsibility, separate from submit/bind")
    intake_ack.add_argument("receipts", nargs="+")
    intake_ack.add_argument("--actor", choices=("orch", "operator"), default="orch")
    intake_claim = intake_actions.add_parser("claim", help="connector: approved -> sending, once")
    intake_claim.add_argument("--draft", required=True)
    intake_claim.add_argument("--version", required=True, type=int)
    intake_show = intake_actions.add_parser("draft-show", help="read-only: one exact draft version including its body")
    intake_show.add_argument("--draft", required=True)
    intake_show.add_argument("--version", required=True, type=int)
    intake_cancel = intake_actions.add_parser("cancel", help="cancel a not-yet-sending draft")
    intake_cancel.add_argument("--draft", required=True)
    intake_cancel.add_argument("--actor", choices=("orch", "operator"), default="operator")
    sub.add_parser("ack").add_argument("task_id")
    rep = sub.add_parser("report")
    rep.add_argument("task_id")
    rep.add_argument("--status", required=True, choices=["done", "blocked"])
    rep.add_argument("--summary", required=True)
    rep.add_argument("--evidence", default="")
    prog = sub.add_parser("progress")
    prog.add_argument("task_id")
    prog.add_argument("text")
    fl = sub.add_parser("flush")
    fl.add_argument("task_id")
    fl.add_argument("--after-pid", type=int)
    fl.add_argument("--turn-log", help=argparse.SUPPRESS)
    ar = sub.add_parser('_auto-retry', help=argparse.SUPPRESS)
    ar.add_argument('task_id')
    ar.add_argument('event_id', type=int)
    ar.add_argument('--database', required=True)
    from . import question_summary
    ask = sub.add_parser("ask", help="ask with an Operator summary and full evidence",
                         description=question_summary.GUIDANCE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ask.add_argument("task_id")
    ask.add_argument("question", help="summary block + ---詳細--- + full evidence; legacy bodies warn but are accepted")
    ver = sub.add_parser("verify")
    ver.add_argument("task_id")
    ver.add_argument("--timeout", type=float)
    sub.add_parser("list")
    quota_parser = sub.add_parser('quota', help='subscription quota observations; never launches inference')
    quota_parser.add_argument('--refresh', action='store_true', help='refresh from Codex session snapshots')
    quota_parser.add_argument('--endpoint', help='existing authorized Codex app-server Unix socket')
    quota_parser.add_argument('--probe', choices=['sol', 'sonnet'], help='bounded native CLI quota read; no model turn')
    quota_parser.add_argument('--binary', help='CLI executable for --probe')
    quota_parser.add_argument('--codex-log', help='collect this completed exec turn from its thread rollout')
    quota_parser.add_argument('--ingest', choices=['claude-statusline', 'claude-sdk'],
                              help='consume an official receipt from stdin; no global config changes')
    goal = sub.add_parser("goal", help="central goals, audit history and markdown snapshot")
    goal_sub = goal.add_subparsers(dest="goal_cmd", required=True)
    from orchd import goals
    for command in ("add", "set"):
        mutation = goal_sub.add_parser(command)
        if command == "add":
            mutation.add_argument("repo")
        else:
            mutation.add_argument("goal_id")
        mutation.add_argument("--actor", default=f"operator:{getpass.getuser()}", help="audit actor; MCP records Orch id")
        mutation.add_argument("--expected-revision", type=int, help="issue_scope.revision; required to replace existing issues")
        mutation.add_argument("--fields", type=json.loads, default={}, help="JSON object; null clears optional fields")
        for field in goals.INPUT_FIELDS:
            if field == "repo":
                continue
            kind = json.loads if field in ("companies", "linked_tasks", "issues") else float if field == "v" else int if field == "j" else str
            mutation.add_argument("--" + field.replace("_", "-"), type=kind, default=argparse.SUPPRESS)
    goal_sub.add_parser("show").add_argument("goal_id")
    goal_list = goal_sub.add_parser("list")
    goal_list.add_argument("--repo")
    goal_list.add_argument("--status", choices=["active", "waiting", "paused", "done"])
    export = goal_sub.add_parser("export")
    export.add_argument("--md", action="store_true", required=True)
    export.add_argument("--repo")
    board = sub.add_parser("board", help="render a private static snapshot, without consuming inbox")
    board.add_argument("--html", required=True, type=Path)
    snapshot = sub.add_parser("snapshot", help="read-only dashboard JSON from a private DB/WAL copy")
    snapshot.add_argument("--json", action="store_true", required=True)
    snapshot.add_argument("--schema-version", type=int, choices=(1, 2), default=1,
                          help="output contract: 1 preserves legacy rows/values (default); 2 opts into lifecycle/receipts")
    snapshot.add_argument("--answered-limit", type=int, default=100,
                          help="newest answered questions to include (default: 100); current/queued are unlimited")
    for name in ("watch", "summary"):
        sub.add_parser(name).add_argument("--since", help="HH:MM today (default: last 30 minutes for watch, all for summary)")
    stats_p = sub.add_parser("stats")
    stats_p.add_argument("--since", help="HH:MM today (local) or ISO-8601 with Z/offset; default all time")
    stats_p.add_argument("--json", action="store_true", help="full detail as JSON instead of the table")
    adopt = sub.add_parser("adopt")
    adopt.add_argument("new_orch")
    adopt.add_argument("task_ids", nargs="*")
    adopt.add_argument("--from", dest="from_orch", help="move all open tasks of this Orch (or limit TASK_IDs to it)")
    adopt.add_argument("--force", action="store_true", help="also move from an alive or unknown owner, and tell it")
    sub.add_parser("close").add_argument("task_id")
    cfg = sub.add_parser("config", help="personal settings in ~/.config/orchd/config.toml (worker-model, projects-dir)")
    cfg_actions = cfg.add_subparsers(dest="config_action", required=True)
    cfg_actions.add_parser("show", help="every setting with its source: flag, env, file or default")
    cfg_actions.add_parser("get").add_argument("key")
    cfg_set = cfg_actions.add_parser("set")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    cfg_actions.add_parser("unset", help="remove the override from the config file (env var, if set, still wins)").add_argument("key")
    doc = sub.add_parser("doctor")
    doc.add_argument("--profile", choices=["example"], help="also check a configurable example layout (accounts, SSH aliases, connectors)")
    doc.add_argument("--json", action="store_true", help="read-only JSON output; never prompt")
    doc.add_argument("--no-input", action="store_true", help="read-only check; skip first-run project setup")
    return parser


def _notify_urgent(con, result):
    """After the receipt is committed: urgent intake wakes its owner now. A failed wake never fails the call;
    the notification stays `uncertain` until explicit transport reconciliation."""
    from orchd import intake_notify
    from orchd.runtime import Runtime
    owner = result.get("owner_orch")
    if not owner or result.get("status") not in ("received", "duplicate", "suggested", "bound", "unchanged"):
        return
    row = con.execute("SELECT 1 FROM intake_notifications WHERE receipt_id=? AND owner_orch=? AND urgency='urgent' "
                      "AND state IN ('pending','failed')", (result.get("receipt_id"), owner)).fetchone()
    if row is not None:
        try:
            result["notify"] = intake_notify.deliver_due(con, Runtime(), only_urgent=True, owner=owner)
        except Exception as error:
            result["notify"] = dict(error=f"{type(error).__name__}: {error}"[:300])


def _live_jobs():
    """Best-effort runtime snapshot; None means unknown (callers then never treat an owner as dead)."""
    from orchd.runtime import Runtime
    try:
        return Runtime().live_jobs()
    except Exception:
        return None


def _intake_main(args):
    """Every outcome is one JSON receipt on stdout; exit 1 for status=failed. A rejected call writes nothing."""
    import sqlite3
    from orchd import config, intake
    con = None
    try:
        action = args.intake_action
        payload = None
        if action in ("submit", "suggest", "decide", "resolve", "draft", "approve", "receipt", "classify"):
            try:
                text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
                payload = json.loads(text)
            except (OSError, UnicodeError, ValueError) as error:
                raise intake.IntakeError("invalid", f"cannot read JSON input: {type(error).__name__}") from None
        producers = config.intake_producers() if action in ("submit", "suggest") else None
        if action == "submit":  # grant check before the DB is even opened: a rejected producer writes nothing
            event = payload if isinstance(payload, dict) else {}
            intake.check_producer(producers, args.producer, event.get("provider"), event.get("account"))
        con = None if action == "reconcile" else store.connect()  # reconcile opens the DB read-only until --apply
        if action == "reconcile":
            result = intake.reconcile_path(store.home() / "orchd.db", apply=args.apply,
                                           backup_dir=store.home() / "backups", jobs=_live_jobs())
        elif action == "submit":
            result = intake.submit(con, payload, producer=args.producer, producers=producers)
            _notify_urgent(con, result)
        elif action == "suggest":
            from orchd import intake_bridge
            result = intake_bridge.suggest(con, args.receipt, payload, producer=args.producer, producers=producers)
            _notify_urgent(con, result)
        elif action == "show":
            from orchd import intake_bridge
            result = intake_bridge.show(con, args.receipt, args.viewer)
        elif action in ("decide", "resolve"):
            from orchd import intake_decide
            result = getattr(intake_decide, action)(con, payload, orch_id=args.orch)
        elif action == "decisions":
            from orchd import intake_decide
            result = intake_decide.decisions(con, args.receipt, orch_id=args.orch, state=args.state)
        elif action == "backlog":
            from orchd import intake_decide
            result = intake_decide.backlog(con, owner=args.owner)  # read-only: no runtime probe
        elif action == "notify-reconcile":
            from . import intake_notify
            result = intake_notify.reconcile(con, args.notification_id, args.attempt,
                                             delivered=args.delivered == "yes", detail=args.detail)
        elif action == "deliver":
            from orchd import intake_notify
            from orchd.runtime import Runtime
            result = dict(status="delivered", **intake_notify.deliver_due(con, Runtime(), only_urgent=args.urgent_only))
        elif action == "pending":
            result = intake.pending(con, args.owner, args.limit, after_id=args.after_id,
                                    high_water_id=args.high_water_id, cursor=args.cursor)
        elif action == "exclude":
            result = intake.exclude(con, args.receipt, reason=args.reason,
                                    rule_version=args.rule_version, actor=args.actor)
        elif action == "bind":
            result = intake.bind_owner(con, args.receipt, args.owner, only_if_unassigned=args.only_if_unassigned)
            _notify_urgent(con, result)
        elif action == "ack":
            result = intake.ack(con, args.receipts, actor=args.actor)
        elif action == "classify":
            result = intake.classify(con, args.receipt, payload)
        elif action == "draft":
            result = intake.draft(con, payload, actor=args.actor)
        elif action == "approve":
            result = intake.approve(con, payload, actor=args.actor)
        elif action == "draft-show":
            result = intake.show_draft(con, args.draft, args.version)
        elif action == "claim":
            result = intake.claim(con, args.draft, args.version)
        elif action == "receipt":
            result = intake.record_receipt(con, payload, actor=args.actor)
        else:
            result = intake.cancel(con, args.draft, actor=args.actor)
    except intake.IntakeError as error:
        result = intake.failure(error)
    except (ValueError, OSError, sqlite3.Error) as error:
        result = dict(status="failed", error_code="invalid", error=str(error))
    finally:
        if con is not None:
            con.close()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result.get("status") == "failed" else 0


# Every leaf subcommand is classified (tests/test_home_migrations.py fails on an unlisted one):
#   write - applies a stale Orch home migration at startup, then runs
#   warn  - read-only: only prints the stale-home warning, never writes the home
#   skip  - does not touch the Orch home rules (init/upgrade manage them; the rest never read the home)
# Plain `list` is warn: core.list_open only reads.
HOME_POLICY = {
    ("mcp",): "write",
    **{("interface", a): "skip" for a in ("register", "submit", "fetch", "ack", "deregister", "list", "rebind")},
    ("orch", "start"): "write", ("orch", "stop"): "write", ("orch", "restart"): "write",  # --dry-run -> warn
    ("orch", "list"): "write", ("orch", "attach"): "write",
    ("orch", "register-codex-probe"): "write",
    ("init",): "skip", ("upgrade",): "skip", ("quota",): "skip", ("doctor",): "skip",
    ("config", "show"): "skip", ("config", "get"): "skip", ("config", "set"): "skip", ("config", "unset"): "skip",
    ("binding",): "write",  # --status -> warn
    ("entry", "thread"): "warn", ("entry", "reconcile"): "write",  # no --apply -> warn
    ("intake", "reconcile"): "write",  # no --apply -> warn
    ("entry", "tag"): "write", ("entry", "send"): "write", ("entry", "ask"): "write",
    ("entry-question", "show"): "warn", ("entry-question", "snooze"): "write", ("entry-question", "un-snooze"): "write",
    ("entry-question", "cancel"): "write", ("entry-question", "supersede"): "write", ("entry-question", "answer"): "write",
    ("task-question", "answer"): "write", ("task-question", "dismiss"): "write",
    **{("intake", a): "write" for a in ("submit", "draft", "approve", "receipt", "classify", "suggest", "deliver",
                                        "notify-reconcile", "decide", "resolve", "exclude", "bind", "ack", "claim", "cancel")},
    **{("intake", a): "warn" for a in ("show", "decisions", "backlog", "pending", "draft-show")},
    ("task", "link"): "write", ("task", "unlink"): "write",
    ("ack",): "write", ("report",): "write", ("progress",): "write", ("flush",): "write",
    ("watch",): "warn", ("summary",): "warn", ("_auto-retry",): "write", ("ask",): "write", ("verify",): "write", ("close",): "write", ("adopt",): "write",
    ("list",): "warn", ("stats",): "warn", ("snapshot",): "warn", ("board",): "warn",
    ("goal", "add"): "write", ("goal", "set"): "write",
    ("goal", "show"): "warn", ("goal", "list"): "warn", ("goal", "export"): "warn",
}
ACTION_DEST = ("interface_action", "orch_action", "entry_action", "question_action", "task_action", "task_question_action",
               "intake_action", "goal_cmd", "config_action")


def _home_policy(args):
    cmd = args.cmd.split()[0]
    action = next((getattr(args, d) for d in ACTION_DEST if getattr(args, d, None)), None)
    policy = HOME_POLICY.get((cmd, action) if action else (cmd,), "write")  # unknown -> write (tests keep the table complete)
    if policy == "write" and (getattr(args, "dry_run", False) or (cmd == "binding" and args.status)
                              or (cmd in ("entry", "intake") and action == "reconcile" and not args.apply)):
        return "warn"
    return policy


def main(argv=None):
    args = build_parser().parse_args(argv)
    policy = _home_policy(args)
    if policy != "skip":  # one gate for every entry point: rules in the Orch home must not lag the installed code (#58)
        from orchd import home_migrations
        for line in home_migrations.ensure_current(write=policy == "write"):
            print(line, file=sys.stderr)
    if args.cmd == "interface":
        from orchd import interface
        import sqlite3
        con = None
        try:
            if args.interface_action == "rebind" and (os.environ.get("ORCHD_ORCH_ID") or os.environ.get("ORCHD_WORKTREE")):
                raise ValueError("interface rebind is Operator-only; run from a trusted local Operator shell")
            con = store.connect()
            if args.interface_action == "list":
                result = interface.list_interfaces(con)
            elif args.interface_action == "rebind":
                result = interface.rebind(con, Runtime(), old_id=args.old_id, new_id=args.new_id, dry_run=args.dry_run)
            else:
                payload = json.load(sys.stdin)
                if not isinstance(payload, dict):
                    raise ValueError("expected JSON object")
                action = getattr(interface, args.interface_action)
                if args.interface_action == "ack":
                    # Identity comes from the calling Orch, never adapter JSON.
                    payload['orch_id'] = os.environ.get("ORCHD_ORCH_ID") or None
                result = action(con, Runtime(), **payload) if args.interface_action == "submit" else action(con, **payload)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except (ValueError, TypeError, OSError, sqlite3.Error) as error:
            print(json.dumps(dict(error=type(error).__name__, message=str(error))))
            return 1
        finally:
            if con is not None:
                con.close()
    if args.cmd == 'quota':
        from orchd import quota
        cache = quota.Cache()
        try:
            if args.probe:
                quota.probe(cache, args.probe, args.binary)
            if args.codex_log:
                quota.collect_codex_turn(cache, args.codex_log)
            if args.ingest:
                cache.claude(json.load(sys.stdin), sdk=args.ingest == 'claude-sdk')
            if args.endpoint:
                from orchd.app_transport import RPC
                rpc = RPC(args.endpoint)
                try:
                    quota.refresh(cache, rpc=rpc)
                finally:
                    rpc.close()
            elif args.refresh:
                quota.refresh(cache)
            print(json.dumps(cache.status(), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, TypeError, AttributeError) as error:
            print(f'orchd quota: {type(error).__name__}', file=sys.stderr)
            return 1

    if args.cmd == "orch":
        args.cmd = f"orch {args.orch_action}"

    if args.cmd == "entry":
        import sqlite3
        from orchd import entry, entry_threads, stats
        con = None
        try:
            if args.entry_action == "thread":
                refs = (args.issue_refs if args.issue_refs is not None else args.issue_ref).split(",")
                result = entry_threads.read(store.home() / "orchd.db", refs, entry_id=args.entry, limit=args.limit)
            elif args.entry_action == "reconcile":
                result = entry.reconcile_path(store.home() / "orchd.db", apply=args.apply,
                                              backup_dir=store.home() / "backups")
            else:
                orch_id = os.environ.get("ORCHD_ORCH_ID")
                if not orch_id:
                    raise ValueError("entry writes require ORCHD_ORCH_ID from the calling Orch")
                con = store.connect()
                if args.entry_action == "tag":
                    result = entry.entry_tag(con, orch_id, args.message_id, args.issue_ref)
                elif args.entry_action == "send":
                    result = entry.send_to_nat(con, Runtime(), orch_id, args.text, issue_ref=args.issue_ref)
                else:
                    result = entry.ask_nat(con, Runtime(), orch_id, args.text, args.task_id,
                                           args.quote_worker_question, issue_ref=args.issue_ref)
            print(json.dumps(result, ensure_ascii=False, indent=1))
            return 0
        except (ValueError, OSError, sqlite3.Error, stats.SnapshotError) as error:
            print(f"orchd entry: {error}", file=sys.stderr)
            return 1
        finally:
            if con is not None:
                con.close()

    if args.cmd == "snapshot":
        import sqlite3
        from orchd import snapshot, stats
        try:
            result = snapshot.read(store.home() / "orchd.db", answered_limit=args.answered_limit,
                                   schema_version=args.schema_version)
        except (ValueError, OSError, sqlite3.Error, stats.SnapshotError) as error:
            print(f"orchd snapshot: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    if args.cmd in ("goal", "board"):
        from orchd import board as board_module, goals, stats
        # Read operations use a private DB+WAL copy. Migrate only that copy so old DBs render too.
        read_only = args.cmd == "board" or args.goal_cmd in ("show", "list", "export")
        snap = con = None
        try:
            if read_only:
                path = store.home() / "orchd.db"
                if not path.exists():
                    raise ValueError(f"no orchd DB at {path}")
                snap = stats.open_snapshot(path)
                copy_path = snap.con.execute("PRAGMA database_list").fetchone()[2]
                snap.con.close()
                snap.con = None
                con = store.connect(copy_path)
            else:
                con = store.connect()
            if args.cmd == "board":
                args.html.write_text(board_module.render(board_module.snapshot(con, Runtime())), encoding="utf-8")
                print(str(args.html))
            elif args.goal_cmd in ("add", "set"):
                if not isinstance(args.fields, dict):
                    raise ValueError("--fields must be a JSON object")
                fields = dict(args.fields)
                fields.update({k: getattr(args, k) for k in goals.INPUT_FIELDS if hasattr(args, k)})
                print(json.dumps(goals.set_goal(con, getattr(args, "goal_id", None), actor=args.actor, expected_revision=args.expected_revision, **fields),
                                 ensure_ascii=False, indent=1))
            elif args.goal_cmd == "show":
                print(json.dumps(goals.get(con, args.goal_id, history=True, include_issues=True), ensure_ascii=False, indent=1))
            elif args.goal_cmd == "list":
                print(json.dumps(goals.list_goals(con, args.repo, args.status), ensure_ascii=False, indent=1))
            else:
                print(goals.export_md(con, args.repo))
            return 0
        except (ValueError, KeyError, OSError, stats.SnapshotError) as error:
            print(f"orchd {args.cmd}: {error}", file=sys.stderr)
            return 1
        finally:
            if con is not None:
                con.close()
            if snap is not None:
                snap.close()

    if args.cmd == "config":  # config file only; never touches the DB
        from orchd import config
        try:
            if args.config_action == "show":
                print(json.dumps(config.show(), ensure_ascii=False, indent=1))
            elif args.config_action == "get":
                print(config.get_value(args.key)[0])
            elif args.config_action == "set":
                config.set_value(args.key, args.value)
                print(f"{args.key} = {args.value} ({config.path()})")
            else:
                config.unset_value(args.key)
                value, source = config.get_value(args.key)
                print(f"{args.key} removed from the config file; effective now {value} ({source})")
        except ValueError as error:
            print(f"orchd config: {error}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "doctor":  # no DB; only interactive first-run setup may save the chosen project root
        from orchd import doctor
        if not args.json and not args.no_input and sys.stdin.isatty():
            from orchd.project_setup import prompt_projects
            try:
                prompt_projects()
            except (OSError, UnicodeError, ValueError) as error:
                print(f"doctor: could not save project settings: {error}", file=sys.stderr)
                return 1
        checks = doctor.Doctor(profile=args.profile).run_all()
        print(json.dumps([c.as_dict() for c in checks], ensure_ascii=False, indent=1) if args.json
              else doctor.render(checks))
        return doctor.exit_code(checks)

    if args.cmd == "upgrade":
        from orchd import upgrade
        code, lines = upgrade.upgrade(migrations_only=args.migrations_only, dry_run=args.dry_run)
        print("\n".join(lines), file=sys.stdout if code == 0 else sys.stderr)
        return code
    if args.cmd == "init":  # files, trust and home-location record; no task DB changes
        from orchd import bootstrap
        try:
            result = bootstrap.init(Runtime(), trust=not args.no_trust, source=args.source)
        except ValueError as error:
            print(f"init: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "mcp":
        from orchd import mcp_server
        mcp_server.serve(role=args.role, entry_id=args.entry)
        return 0
    if args.cmd == "stats":  # reads a temp copy of the DB: never opens, migrates or creates anything next to the real one
        from orchd import stats
        path = store.home() / "orchd.db"
        if not path.exists():
            print(f"no orchd DB at {path}", file=sys.stderr)
            return 1
        try:
            with stats.open_snapshot(path) as snap:  # private copy of the DB+WAL; the source is only read
                report = stats.stats(snap.con, stats.parse_since(args.since) if args.since else None)
        except stats.SnapshotError as err:
            print(f"orchd stats: {err}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else stats.format_table(report))
        return 0
    if args.cmd in ("orch list", "orch attach", "orch register-codex-probe"):
        from orchd import inventory
        if not (store.home() / "orchd.db").exists():
            print("orchd: no registry DB", file=sys.stderr)
            return 1
        con = store.connect()
        try:
            if args.cmd == "orch register-codex-probe":
                from orchd import codex_orch
                print(json.dumps(codex_orch.register(con, args.orch_id, args.pid, args.endpoint)))
            elif args.cmd == "orch attach":
                inventory.attach(con, Runtime(), args.orch_id, viewer=args.viewer)
            elif args.restore:
                inventory.restore(con, args.restore)
                print(f"Restored {args.restore}; use --all to inspect dead/unknown Orchs.")
            else:
                report = inventory.list_orchs(con, Runtime(), observe=True)
                print(json.dumps(report, ensure_ascii=False, indent=1) if args.json
                      else inventory.render(report, all=args.all))
        except (ValueError, RuntimeError, OSError) as err:
            print(f"orchd {args.cmd}: {err}", file=sys.stderr)
            return 1
        finally:
            con.close()
        return 0
    if args.cmd == "orch restart":
        from orchd import orch_restart, stats
        con = snap = None
        try:
            if args.dry_run:
                # Plan against a private DB/WAL snapshot, including any needed schema migration.
                snap = stats.open_snapshot(store.home() / "orchd.db")
                copy_path = snap.con.execute("PRAGMA database_list").fetchone()[2]
                snap.con.close()
                snap.con = None
                con = store.connect(copy_path)
            else:
                con = store.connect()
            result = orch_restart.restart(con, Runtime(), args.old_id, args.model, args.dry_run)
            print(json.dumps(result, ensure_ascii=False, indent=1))
            if result["status"] == "done":
                print(f"New Orch: {result['new_orch']}; first prompt: {result['first_prompt']}")
            return 0 if result["status"] in ("done", "dry-run") else 1
        except (ValueError, OSError, stats.SnapshotError) as error:
            print(f"orch restart select: {error}; inspect with `orchd orch list --all`", file=sys.stderr)
            return 1
        finally:
            if con is not None:
                con.close()
            if snap is not None:
                snap.close()
    if args.cmd == "intake":
        return _intake_main(args)
    if args.cmd == "task":
        import sqlite3
        from orchd import issue_links
        con = None
        try:
            con = store.connect()
            options = dict(actor=args.actor, idempotency_key=args.idempotency_key)
            result = (issue_links.link(con, args.task, args.ref, **options) if args.task_action == "link"
                      else issue_links.unlink(con, args.task, **options))
        except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as error:
            result = dict(status="failed", task_id=args.task, error=str(error))
        finally:
            if con is not None:
                con.close()
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 1 if result["status"] == "failed" else 0
    if args.cmd == "task-question":
        import sqlite3
        from orchd import task_question_answers
        con = None
        try:
            con = store.connect()
            if args.task_question_action == "dismiss":
                result = task_question_answers.dismiss(con, args.task, args.question_id, args.reason,
                                                       args.idempotency_key, actor=args.actor)
            else:
                text = args.text_file.read_text(encoding="utf-8")
                result = task_question_answers.submit(con, Runtime(), args.task, args.question_id, text,
                                                     args.idempotency_key, actor=args.actor)
        except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as error:
            result = dict(status="failed", delivered=0,
                          pending=store.pending_answer_count(con, args.task) if con else None,
                          error=str(error))
        finally:
            if con is not None:
                con.close()
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 1 if result["status"] == "failed" else 0

    con, rt = store.connect(args.database if args.cmd == '_auto-retry' else None), Runtime()
    if args.cmd in ("watch", "summary"):
        from orchd import watch
        since = None
        if args.since:
            since = time.mktime(time.strptime(time.strftime("%Y-%m-%d ") + args.since, "%Y-%m-%d %H:%M"))
        if args.cmd == "watch":
            try:
                watch.watch(con, since if since is not None else time.time() - 1800)
            except KeyboardInterrupt:
                pass
        else:
            print(json.dumps(watch.summary(con, since or 0), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "orch start":
        row = core.start_orch(con, rt, args.model)
        print(f"orch {row['id']} ({row['model']}) job {row['job_id']}", flush=True)
        if not args.no_attach:
            rt.attach(row["job_id"])
    elif args.cmd == "orch stop":
        core.stop_orch(con, rt, args.orch_id)
        print(f"stopped {args.orch_id}")
    elif args.cmd == "binding":
        from orchd import entry
        try:
            if args.status:
                result = entry.snapshot(con, rt, args.entry)
            elif args.to:
                result = entry.bind(con, rt, args.to, args.entry, force=True)
            else:
                result = entry.binding(con, rt, lambda: core.start_orch(con, rt, "opus"), args.new, args.entry)
        except (ValueError, RuntimeError, OSError) as error:
            print(f"binding: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.cmd == "entry-question":
        from orchd import entry
        try:
            if args.question_action == "show":
                result = entry.question_show(con, args.entry, args.question_id)
            elif args.question_action == "answer":
                result = entry.submit_answer(con, rt, args.entry, args.question_id, args.expected_version,
                                             args.expected_state, args.text, args.idempotency_key, orch_id=args.orch_id)
            elif args.question_action == "supersede":
                bound = entry.get_entry(con, args.entry)
                result = entry.ask_nat(con, rt, bound["orch_id"], args.text, args.task_id,
                                       supersedes=args.question_id, expected_version=args.expected_version,
                                       expected_state=args.expected_state, reason=args.reason,
                                       actor=f"operator:{getpass.getuser()}")
            else:
                result = entry.question_transition(
                    con, rt, args.entry, args.question_id, args.expected_version, args.expected_state,
                    {"snooze": "snoozed", "un-snooze": "queued", "cancel": "cancelled"}[args.question_action],
                    actor=f"operator:{getpass.getuser()}", reason=getattr(args, "reason", None),
                    revisit_at=getattr(args, "revisit_at", None), note=getattr(args, "note", None))
        except (ValueError, RuntimeError, OSError) as error:
            print(f"entry-question: {error}", file=sys.stderr)
            return 1
        finally:
            con.close()
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.cmd == "ack":
        core.ack(con, args.task_id)
        print(f"acked {args.task_id}")
    elif args.cmd == "report":
        woke = core.report(con, rt, args.task_id, args.status, args.summary, args.evidence)
        print(f"reported {args.task_id}" + ("" if woke else " (stored; waking Orch failed, it will see it in list_open)"))
    elif args.cmd == "progress":
        woke = core.progress(con, rt, args.task_id, args.text)
        print(f"progress sent on {args.task_id}" + ("" if woke else " (stored; waking Orch failed)"))
    elif args.cmd == "flush":
        result = core.auto_flush(con, rt, args.task_id, args.after_pid, turn_log=args.turn_log)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["status"] == "failed" else 0
    elif args.cmd == '_auto-retry':
        result = core.auto_retry(con, rt, args.task_id, args.event_id)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result['status'] == 'failed' else 0
    elif args.cmd == "ask":
        woke = core.ask(con, rt, args.task_id, args.question)
        print(f"asked on {args.task_id}; wait for an [orchd answer {args.task_id}] message"
              + ("" if woke else " (stored; waking Orch failed)"))
    elif args.cmd == "verify":
        from orchd import verify
        try:
            result = verify.run(con, args.task_id, timeout=args.timeout)
        except (ValueError, KeyError) as error:
            print(f"verify refused: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result["passed"] else 1
    elif args.cmd == "list":
        print(json.dumps(core.list_open(con, rt), ensure_ascii=False, indent=1))
    elif args.cmd == "adopt":
        try:
            print(json.dumps(core.adopt(con, rt, args.new_orch, args.task_ids, args.from_orch, args.force),
                             ensure_ascii=False, indent=1))
        except (ValueError, KeyError) as error:
            print(f"adopt refused: {error}", file=sys.stderr)
            return 1
    elif args.cmd == "close":
        print(json.dumps(core.close(con, rt, args.task_id), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
