"""orchd: Orch MCP server and the worker/operator command line.

  orchd mcp [--role orch|entry] [--entry NAME]   run the MCP server for an Orch session, or for the Desktop entry (stdio)
  orchd orch start [--model opus|sonnet] [--no-attach]   start a Claude Orch, then attach to it
  orchd orch stop ID                          stop a Claude Orch
  orchd orch restart [OLD_ID] [--model opus|sonnet] [--dry-run]
                                              stop, confirm death, start, adopt, and rebind Desktop
  orchd init [--from OLD_ORCH_HOME] [--no-trust]   create ~/orch/home and ~/orch/interface, trust them in Codex
  orchd upgrade                               reinstall the uv-installed orchd at its source's newest commit
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
  orchd list                                  open tasks, worker/Orch health, unread notification failures
  orchd goal add|set|show|list|export --md      central goals, audit history, markdown snapshot
  orchd board --html PATH                     private static project board, no inbox consumption
  orchd watch [--since HH:MM]                 live timeline of Orch <-> worker messages
  orchd summary [--since HH:MM]               per Orch: workers, models, questions, parallelism, tokens
  orchd stats [--since T] [--json]            per Orch x task_type: counts, rework, source-backed tokens, cost estimate
  orchd doctor [--profile example] [--json]       read-only machine check; exit 1 required fail, 2 required unknown
  orchd adopt NEW_ORCH [TASK_ID...] [--from OLD_ORCH] [--force]   move open tasks to another Orch (operator only)
  orchd close ID                              stop a task's worker and clean its worktree if safe

Orch actions require an explicit subcommand; see `orchd orch --help`.
"""
import argparse
import getpass
import json
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
    attachment = actions.add_parser("attach", help="attach an existing Claude Orch")
    attach_options(attachment)

    init = sub.add_parser("init")
    init.add_argument("--from", dest="source", help="copy what an old Orch home kept (e.g. ~/projects/orch); never overwrites")
    init.add_argument("--no-trust", action="store_true", help="do not write Codex trust or check Claude trust")
    sub.add_parser("upgrade")
    binding = sub.add_parser("binding")
    binding.add_argument("--entry", default="desktop")
    how = binding.add_mutually_exclusive_group()
    how.add_argument("--new", action="store_true", help="start a new Claude Orch and bind the interface to it")
    how.add_argument("--to", metavar="ORCH_ID", help="bind the interface to this live Claude Orch")
    how.add_argument("--status", action="store_true", help="read-only: binding, Orch health, questions, delivery")
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
    ask = sub.add_parser("ask")
    ask.add_argument("task_id")
    ask.add_argument("question")
    ver = sub.add_parser("verify")
    ver.add_argument("task_id")
    ver.add_argument("--timeout", type=float)
    sub.add_parser("list")
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
        mutation.add_argument("--fields", type=json.loads, default={}, help="JSON object; null clears optional fields")
        for field in goals.INPUT_FIELDS:
            if field == "repo":
                continue
            kind = json.loads if field in ("companies", "linked_tasks") else float if field == "v" else int if field == "j" else str
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
    doc = sub.add_parser("doctor")
    doc.add_argument("--profile", choices=["example"], help="also check a configurable example layout (accounts, SSH aliases, connectors)")
    doc.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "orch":
        args.cmd = f"orch {args.orch_action}"

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
                print(json.dumps(goals.set_goal(con, getattr(args, "goal_id", None), actor=args.actor, **fields),
                                 ensure_ascii=False, indent=1))
            elif args.goal_cmd == "show":
                print(json.dumps(goals.get(con, args.goal_id, history=True), ensure_ascii=False, indent=1))
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

    if args.cmd == "doctor":  # read-only; must not touch the DB
        from orchd import doctor
        checks = doctor.Doctor(profile=args.profile).run_all()
        print(json.dumps([c.as_dict() for c in checks], ensure_ascii=False, indent=1) if args.json
              else doctor.render(checks))
        return doctor.exit_code(checks)

    if args.cmd == "upgrade":  # program only; never touches the DB or ~/orch
        from orchd import upgrade
        code, lines = upgrade.upgrade()
        print("\n".join(lines), file=sys.stdout if code == 0 else sys.stderr)
        return code
    if args.cmd == "init":  # files and trust only; never touches the DB
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
    if args.cmd in ("orch list", "orch attach"):
        from orchd import inventory
        if not (store.home() / "orchd.db").exists():
            print("orchd: no registry DB", file=sys.stderr)
            return 1
        con = store.connect()
        try:
            if args.cmd == "orch attach":
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
    con, rt = store.connect(), Runtime()
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
        result = core.auto_flush(con, rt, args.task_id, args.after_pid)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["status"] == "failed" else 0
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
