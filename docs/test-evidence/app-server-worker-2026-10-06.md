# Opt-in app-server worker evidence (task b5e9c2a7)

Implementation follows spike 058ab680 design (b). Tested on macOS with
codex-cli 0.160.1, tmux 3.7c and an isolated Python 3.13 environment.

## Real-Codex acceptance

Command: `python3 scripts/app_server_e2e.py` (checkout venv on PATH).
Final run output, after the startup/shutdown identity fixes:

```text
PASS task app-server; active thread 01a10eef-21d1-77c0-80f0-e02b83dc59b1 turn 01a10eef-2782-7353-8b78-d45a2bfbeab5
PASS mid-turn native remote TUI attach; answer FIFO queued
PASS injected line seen as same-turn userMessage 01a10eef-2782-7353-8b78-d45a2bfbeab5
PASS viewer closed; worker continues
PASS queued answer auto-flushed exactly once; deliveries=2 answers=1
PASS close: no server/supervisor/viewer/tmux/marked child processes
PASS isolated DB/CODEX_HOME; global config unchanged; no live orchd tasks accessed
```

The script uses a temporary git worktree, database, CODEX_HOME and private tmux socket.
It copies authentication only, deletes it with the temporary home, and hashes the global
Codex config before and after. It does not start/stop the shared daemon. The final close check also compares the stored
PID/birth identities for the native viewer and private tmux server, not only socket readiness. Earlier failed exploratory
runs invoked the script's finally cleanup; two earlier close checks found a
main-worktree fixture mistake and a PID-exit race, both fixed before the run above.

## Unit suites

Command: `python3 -m unittest discover -s tests` with the checkout venv on PATH.
Final serial run (including every new guard/receipt regression):

```text
Ran 667 tests in 85.865s

OK
```

One earlier 666-test run encountered a native macOS psutil `proc_environ` SystemError
in the unmodified `WorkerProcessesTest.test_close_cleans_orphans_after_worker_already_exited`
Claude scanner path. No cause was established and that existing scanner was not changed.
The focused worker-process suite then passed (`Ran 5 tests in 9.840s`, `OK`), the serial
666-test suite passed, and the final 667-test run above passed. Existing Python 3.13
ResourceWarnings were also emitted. This transient scanner failure remains a known limitation.

## Default and lifecycle boundaries

`core.dispatch` defaults `backend="exec"`; only its explicit `backend == "app-server"`
branch enters the new supervisor. The additive task-column migration defaults old rows to
exec. `AppViewerTest.test_default_dispatch_and_migration_remain_exec` asserts the default
code path and MCP default. Existing Claude transport still uses `socket`; the new UDS has
separate endpoint/control fields and birth identities.

Busy answers/followups remain FIFO, and guarded completion/reconnect schedules delivery once.
A viewer owns normal input delivery until every registered native TUI exits. Explicit interrupt
waits for the matching interrupted turn before starting the correction/FIFO turn. An old
interrupt snapshot cannot cancel a newer turn that already received the correction via auto-flush. Delivery
journal states are sending/delivered/rejected/uncertain; uncertain pending is never blindly
resent. Retry/close use stored process identity and bounded targeted shutdown, preserving the
worktree and pending on unconfirmed stop. Cross-provider receipt failure resets backend metadata
for the actual replacement and preserves pending.

No running orchd service, existing worker session, global Codex config or shared daemon was
modified. Live DB access was read-only for the spike specification; this task's own authorized
ack/progress/ask/report use the installed CLI. All implementation/E2E task creation used isolated
DBs. No code was installed into the running service.

## Limits and later Desktop work

- Native remote TUI was exercised in a private tmux PTY; actual Ghostty window launch/focus was
  not GUI-tested. The Ghostty command/wrapper path has unit coverage and opens a new window.
- A crashed server is not automatically restarted. Missing history receipt evidence stays uncertain
  and needs inspection/explicit retry. RPC + SQLite are not an exactly-once transaction.
- Worker permissions/env/brief are preserved, but full MCP/browser capability integration and
  long-running/multi-worker resource behavior were not exercised. Native TUI model/settings changes
  are not reconciled into task model bookkeeping; structured FIFO turns restore task policy.
- Runtime directories/logs are retained for diagnosis. Native interrupt cancels Codex-owned tools;
  detached marked jobs are cleaned on retry/close, not promised killed by interrupt.
- The later Desktop foreground tool must accept a task id/expected generation, enforce the entry's
  binding/ownership, and delegate to this viewer path. No arbitrary command/path/socket inputs;
  unknown/dead/superseded attempts must be visible. Window reuse/focus needs its own acceptance.
