# GPT Desktop computer-use worker (#84)

Status: Phase 1 home setup implemented; Phase 2 backend proposed only. Investigated
2026-10-29. No Desktop sessions were launched, services restarted, credentials
inspected, or live session homes initialized during this investigation.

## Session home

Keep code in orchd. `orchd init` (the setup step after package installation)
creates `~/orch/gpt-computer-use-worker/AGENTS.md` and `reports/`, parallel to
`home/` and `interface/`. `ORCHD_ROOT` relocates all three. The worker rules use
the existing versioned managed block, byte-preserving replacement and state-dir
backups. Init refreshes this worker's block on repeat runs. Upgrade registers a
new `computer-use-rules-v1` step and remembers its initialized path.

These instructions are policy, not technical enforcement. Creating the home
does not establish an aiagent account, grant GUI permissions, or add a backend.

## Launch and attach findings

- The installed `codex app --help` accepts a workspace PATH. It was read without
  launching the app. Official [CLI documentation](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
  also documents opening a workspace on macOS (Windows prints the path).
- Official [app commands](https://learn.chatgpt.com/docs/reference/commands)
  document `codex://new?path=<encoded-absolute-path>&prompt=<encoded-text>` and
  `codex://threads/<thread-id>`. The new-chat link preloads the composer; it does
  not submit the prompt. These are candidates for manual launch and view_worker,
  not proof of automated dispatch or aiagent identity selection.
- Official [app-server documentation](https://learn.chatgpt.com/docs/app-server)
  supports starting/resuming threads with a cwd, sending turns, and receiving
  events. This does not establish a supported attachment to the Desktop-owned
  aiagent session or that app-server workers have Desktop computer-use tools.
  Existing orchd app-server viewer opens a terminal, which does not meet #84.

No supported end-to-end Desktop dispatch/answer automation hook was established
by this read-only investigation. URL handling, thread focus while busy, account
selection, sidebar visibility, tool availability, and reconnect behavior were
not tested in the installed Desktop build. Do not substitute CLI execution or
app-server for that missing validation.

## Proposed backend (requires Operator approval)

Introduce an explicit `desktop-computer-use` backend only after a harmless
Desktop capability probe passes. Keep the current exec/app-server defaults.
Start with an Operator-assisted pilot: open the home and prefilled task prompt,
let Operator verify aiagent identity and send it, then register the actual
Desktop thread ID. Fully automated dispatch needs a supported host hook for
send, receive, interrupt, and live identity; opening a URL is insufficient.

Persist task ID, Desktop thread ID, home, account identity reference (no secrets),
connection generation, and dispatch receipt. Acquire one lease for this GUI
session to avoid concurrent tasks controlling the same screen. Record a pending
dispatch before opening the app; mark acknowledged only on a matching worker ack.
A timeout produces a visible diagnostic, never a blind second send. Reconnection
must reconcile the same task/thread and receipt before continuing; an app crash
or missing session pauses delivery without replaying uncertain GUI actions.

### CLI return channel and answers

The Desktop worker runs `orchd ack`, `progress`, `ask`, and `report` with its
explicit task ID. Existing `core.py` stores task-linked events and wakes Orch;
this direction can reuse the current CLI if its executable and state directory
are available to the Desktop session. Pass an absolute executable and explicit
state location from dispatch (outside the repo); do not assume the Desktop app
inherits the dispatching shell. Verify the selected state database and task
ownership in the pilot before any GUI operation.

Answers need a new backend route: today's answer logic resumes exec workers or
flushes an app-server supervisor. Queue answers durably with delivery IDs bound
to the registered Desktop thread/generation. Mark delivered only after a receipt;
separately acknowledge consumption so reconnect can deduplicate. The manual
pilot requires Operator to relay answers into that same chat; it must not claim
automatic answer delivery. While waiting, retain the session lease and task's
question state. Interruption must stop GUI execution and invalidate outstanding
action approvals before retry. Do not infer task completion from an idle app.

### view_worker

Resolve the stored technical thread ID and open its documented thread deep link.
Opening only the workspace cannot identify the task's chat. Validate that the
link reveals the same sidebar session both while busy and while awaiting an
answer. Missing app, stale/unregistered ID, or unsupported URL handling must
return a clear unavailable diagnostic without launching a replacement worker.

### Safety and validation gate

Bind approval to task, thread generation, destination and exact action/content;
changed content or navigation invalidates it. Keep allowlisted apps/sites,
aiagent-only accounts, Operator-only credential/payment entry, and sanitized
step screenshots under `reports/<task-id>/`. Never include secrets in prompts,
URLs or evidence. AGENTS.md alone cannot enforce these; the future host adapter
must gate actions and evidence capture or disclose its limitations and stop.

Before accepting automated dispatch, demonstrate a harmless GUI task with ack,
progress, question, answer and report in one Desktop sidebar session; test busy
and waiting focus, duplicate delivery, crash/reconnect, stale identity, missing
app, unsupported hooks, cancellation, and changed-action approval invalidation.
The backend remains unimplemented pending Operator's decision and a validated
Desktop transport.
