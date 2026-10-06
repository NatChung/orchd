# PR #78 follow-up validation (task 5c10d9b8)

Reviewed against the full task 70ff82dc review of `be2abd9`.

- B1: websocket is imported only when constructing RPC. A subprocess blocks
  `websocket` in `sys.modules`, imports core and dispatches the default exec backend.
- B2: health checks stored process identities before attempting validated control IPC;
  stop uses identity-based cleanup without requiring an endpoint directory. Tests cover
  missing directory/socket, close, retry to exec and a live owned process after directory loss.
  Control IPC still enforces directory ownership, mode and endpoint validation.
- M1: app_event rows retain only method, thread/turn ids and item/turn id/type/status.
  The E2E reads steer content transiently via RPC history rather than storing it in SQLite.
- M2: uncertain sends schedule periodic history reconciliation even on a connected RPC;
  only a matching client id acknowledges delivery. No uncertain send is automatically resent.
- M3: confirmed interrupt followed by delivery-lock timeout arms deferred flush and
  returns queued with pending retained.
- gh-wrapper fixtures clear inherited GIT_CONFIG variables before worker_env assertions.
  The separate parent-entry preservation test still verifies intentional inheritance.

Commands used the isolated checkout `.venv/bin` on PATH (Python 3.13.15,
psutil 7.2.2, websocket-client 1.9.2):

```text
python3 -m unittest discover -s tests
Ran 676 tests in 93.869s
OK

GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=test.inherited GIT_CONFIG_VALUE_0=worker \
  python3 -m unittest discover -s tests
Ran 676 tests in 93.249s
OK

python3 -m unittest tests.test_app_review
Ran 9 tests in 0.221s
OK
```

The regression suite was run before the fix: 8 tests, 3 failures and 4 errors,
including the blocked-websocket import and missing-runtime close/retry/health symptoms.
The ninth regression adds a real private process group cleanup assertion.
Existing Python 3.13 ResourceWarnings appeared in the full suites.

An intermediate E2E failed because its transient history read preceded the steer
update; the script now waits for the matching turn's marker. A subsequent run passed
steer, viewer and FIFO assertions but close saw `process PID not found` while full
suites were running concurrently. No scanner change was made; the final E2E runs alone.

No live orchd tasks/service or global configuration were modified. This task's
ack/progress/ask/report use the authorized installed worker CLI. No merge performed.

Final isolated real-Codex E2E: `python3 scripts/app_server_e2e.py`, exit 0.

```text
PASS task app-server; active thread 01a10f01-3787-7d92-9b78-61c0b87b4af2 turn 01a10f01-3bff-72e0-b0ea-c7a9b9f7a7c5
PASS mid-turn native remote TUI attach; answer FIFO queued
PASS injected line seen as same-turn userMessage 01a10f01-3bff-72e0-b0ea-c7a9b9f7a7c5
PASS viewer closed; worker continues
PASS queued answer auto-flushed exactly once; deliveries=2 answers=1
PASS close: no server/supervisor/viewer/tmux/marked child processes
PASS isolated DB/CODEX_HOME; global config unchanged; no live orchd tasks accessed
```
