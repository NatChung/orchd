#!/usr/bin/env python3
"""List registered agents or attach to a native agent in the current terminal."""
import argparse
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys

REPO = Path(__file__).resolve().parents[1]

# Reuse the CLI's PID/start-time checks without opening its writable database.
loader = importlib.machinery.SourceFileLoader("agentctl_view_runtime", str(REPO / "bin/agentctl"))
spec = importlib.util.spec_from_loader(loader.name, loader)
ctl = importlib.util.module_from_spec(spec)
loader.exec_module(ctl)


def runtime_status(con, worker):
    if worker["backend"] == "tmux":
        try:
            return "running" if ctl.tmux_alive(worker) else "stopped"
        except OSError:
            return "unknown"
    record = ctl.native.row(con, worker["id"])
    process_alive = ctl.native.alive(ctl, record)
    watcher_alive = ctl.native.alive(ctl, record, "watcher_pid")
    if not process_alive and not watcher_alive:
        return "stopped"
    if record["stopping"]:
        return "stopping"
    return "running" if process_alive and watcher_alive else "inactive"


def list_agents(con):
    rows = con.execute("SELECT * FROM workers ORDER BY role='orchestrator' DESC, id").fetchall()
    rows = [row for row in rows if runtime_status(con, row) == "running"]
    if not rows:
        print("No running agents.")
        return
    table = [["ID", "ROLE", "PROVIDER", "BACKEND", "STATE", "CWD"]]
    table += [[row["id"], "orch" if row["role"] == "orchestrator" else row["role"],
               row["kind"], row["backend"], row["state"], row["cwd"]] for row in rows]
    widths = [max(len(row[i]) for row in table) for i in range(5)]
    for row in table:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)) + "  " + row[-1])


def choose_worker(con):
    rows = con.execute(
        "SELECT * FROM workers WHERE role='worker' AND backend='native' ORDER BY id"
    ).fetchall()
    rows = [row for row in rows if runtime_status(con, row) == "running"]
    if not rows:
        raise RuntimeError("No running native workers; register and start a worker first")
    for number, row in enumerate(rows, 1):
        print(f"{number}. {row['id']} [{row['kind']}, {row['backend']}, {row['state']}] {row['cwd']}")
    while True:
        try:
            choice = input("Worker number (q to cancel): ").strip()
        except EOFError:
            return None
        if choice.lower() in {"q", "quit", ""}:
            return None
        if choice.isdecimal() and 1 <= int(choice) <= len(rows):
            return rows[int(choice) - 1]["id"]
        print(f"Enter a number from 1 to {len(rows)}, or q.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["foreground", "list-agents"])
    parser.add_argument("worker", nargs="?", help="registered worker ID, including orch")
    parser.add_argument("--pick-worker", action="store_true", help="choose a running native worker")
    args = parser.parse_args()
    listing = args.action == "list-agents"
    if listing and (args.worker or args.pick_worker):
        parser.error("listing does not accept a worker ID or --pick-worker")
    if not listing and bool(args.worker) == args.pick_worker:
        parser.error("provide either a worker ID or --pick-worker")
    home = Path(os.environ.get("AGENTCTL_HOME") or REPO / "sandbox/state").resolve()
    # Read-only: this helper never creates, restarts, or stops an agent.
    with sqlite3.connect((home / "agentctl.db").as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        if listing:
            list_agents(con)
            return
        if args.pick_worker:
            args.worker = choose_worker(con)
            if args.worker is None:
                return
        worker = con.execute("SELECT * FROM workers WHERE id=?", (args.worker,)).fetchone()
        native = con.execute("SELECT * FROM native_sessions WHERE worker_id=?", (args.worker,)).fetchone()
    if not worker or worker["backend"] != "native" or not native:
        raise RuntimeError("A registered native agent is required")
    env = dict(os.environ, AGENTCTL_HOME=str(home))
    attach = [str(REPO / "bin/agentctl"), "worker", "attach", args.worker]

    print("Attach in this terminal; Ctrl+Z returns to the shell. "
          "If suspended, use fg to return.", flush=True)
    os.execvpe(attach[0], attach, env)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        print(getattr(exc, "stderr", None) or str(exc), file=sys.stderr)
        sys.exit(1)
