"""orchd: Orch MCP server and the worker/operator command line.

  orchd mcp [--role orch|entry] [--entry NAME]   run the MCP server for an Orch session, or for the Desktop entry (stdio)
  orchd orch [--model opus|sonnet] [--no-attach]   start a Claude Orch, then attach to it
  orchd orch-stop ID                          stop a Claude Orch
  orchd init [--from OLD_ORCH_HOME] [--no-trust]   create ~/orch/home and ~/orch/interface, trust them in Codex
  orchd interface [--new]                     bind ~/orch/interface to a live Claude Orch (starts one if none)
  orchd entry-bind ORCH_ID [--entry NAME] [--force]   bind the Desktop entry to one Claude Orch (operator only)
  orchd entry-status [--entry NAME]           read-only entry binding, Orch health, questions and delivery counts
  orchd ack ID                                worker: acknowledge a task
  orchd report ID --status done|blocked --summary S [--evidence E]
  orchd progress ID "text"                    worker: interim update to the Orch; the task keeps running
  orchd ask ID "question with full preview"   worker: ask Orch/Nat and wait for an answer
  orchd verify ID [--timeout S]               verifier worker: rerun the verified task's locked command at its locked SHA
  orchd orchs                                 read-only Orch inventory as JSON
  orchd list                                  open tasks, worker/Orch health, unread notification failures
  orchd watch [--since HH:MM]                 live timeline of Orch <-> worker messages
  orchd summary [--since HH:MM]               per Orch: workers, models, questions, parallelism, tokens
  orchd stats [--since T] [--json]            per Orch x task_type: counts, rework, source-backed tokens, cost estimate
  orchd doctor [--profile nat] [--json]       read-only machine check; exit 1 required fail, 2 required unknown
  orchd adopt NEW_ORCH [TASK_ID...] [--from OLD_ORCH] [--force]   move open tasks to another Orch (operator only)
  orchd close ID                              stop a task's worker and clean its worktree if safe
"""
import argparse
import json
import sys
import time

from orchd import core, store
from orchd.runtime import DEFAULT_ORCH_MODEL, ORCH_MODELS, Runtime


def main(argv=None):
    parser = argparse.ArgumentParser(prog="orchd", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--role", choices=["orch", "entry"], default="orch")
    mcp.add_argument("--entry", default="desktop", help="entry name (with --role entry)")
    orch = sub.add_parser("orch")
    orch.add_argument("--model", default=DEFAULT_ORCH_MODEL,
                      choices=list(ORCH_MODELS))
    orch.add_argument("--no-attach", action="store_true")
    sub.add_parser("orch-stop").add_argument("orch_id")
    init = sub.add_parser("init")
    init.add_argument("--from", dest="source", help="copy what an old Orch home kept (e.g. ~/projects/orch); never overwrites")
    init.add_argument("--no-trust", action="store_true", help="do not write Codex trust or check Claude trust")
    iface = sub.add_parser("interface")
    iface.add_argument("--new", action="store_true", help="the bound Orch is offline: start a new one and rebind")
    bind = sub.add_parser("entry-bind")
    bind.add_argument("orch_id")
    bind.add_argument("--entry", default="desktop")
    bind.add_argument("--force", action="store_true", help="rebind an entry already bound to another Orch")
    sub.add_parser("entry-status").add_argument("--entry", default="desktop")
    sub.add_parser("ack").add_argument("task_id")
    rep = sub.add_parser("report")
    rep.add_argument("task_id")
    rep.add_argument("--status", required=True, choices=["done", "blocked"])
    rep.add_argument("--summary", required=True)
    rep.add_argument("--evidence", default="")
    prog = sub.add_parser("progress")
    prog.add_argument("task_id")
    prog.add_argument("text")
    ask = sub.add_parser("ask")
    ask.add_argument("task_id")
    ask.add_argument("question")
    ver = sub.add_parser("verify")
    ver.add_argument("task_id")
    ver.add_argument("--timeout", type=float)
    sub.add_parser("list")
    sub.add_parser("orchs")
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
    doc.add_argument("--profile", choices=["nat"], help="also check Nat's own machine layout (accounts, SSH aliases, connectors)")
    doc.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "doctor":  # read-only; must not touch the DB
        from orchd import doctor
        checks = doctor.Doctor(profile=args.profile).run_all()
        print(json.dumps([c.as_dict() for c in checks], ensure_ascii=False, indent=1) if args.json
              else doctor.render(checks))
        return doctor.exit_code(checks)

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
    if args.cmd == "orchs":
        from orchd import inventory, stats
        try:
            with stats.open_snapshot(store.home() / "orchd.db") as snap:
                report = inventory.list_orchs(snap.con, Runtime())
        except stats.SnapshotError as err:
            print(f"orchd orchs: {err}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
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
    if args.cmd == "orch":
        row = core.start_orch(con, rt, args.model)
        print(f"orch {row['id']} ({row['model']}) job {row['job_id']}", flush=True)
        if not args.no_attach:
            rt.attach(row["job_id"])
    elif args.cmd == "orch-stop":
        core.stop_orch(con, rt, args.orch_id)
        print(f"stopped {args.orch_id}")
    elif args.cmd == "interface":
        from orchd import entry
        try:
            result = entry.interface(con, rt, lambda: core.start_orch(con, rt, "opus"), args.new)
        except ValueError as error:
            print(f"interface: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.cmd in ("entry-bind", "entry-status"):
        from orchd import entry
        try:
            result = (entry.bind(con, rt, args.orch_id, args.entry, args.force) if args.cmd == "entry-bind"
                      else entry.snapshot(con, rt, args.entry))
        except ValueError as error:
            print(f"{args.cmd} refused: {error}", file=sys.stderr)
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
