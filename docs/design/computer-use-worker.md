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

## Proposed backend: manually opened session pulls work

Operator reports manually opening the project and a session in the GPT Desktop
App, likely in `~/orch/gpt-computer-use-worker`. This is supplied information;
the session, folder, identity and app capabilities were not inspected. The
backend must not launch, replace, or delete it. A long-lived Desktop session is
the execution surface; orchd owns a durable queue for that registered worker.
The session may process multiple tasks over its lifetime, one task at a time.
This differs from today's task-scoped worker process lifecycle and needs an
explicit backend route rather than pretending the session is an exec worker.

### Notification: pull first, push optional

Recommend bounded long polling through the orchd CLI while the Desktop agent
has an active turn. A proposed `orchd desktop-worker poll --wait 30` blocks until
an eligible task, answer, cancellation, or the timeout arrives, then returns a
JSON envelope. On an empty timeout the agent immediately calls it again, staying
in its current turn. The CLI reads a durable queue; it does not generate model
work for each database check. After completing a task the session resumes its
poll loop. Queue arrival does not require opening or submitting a Desktop chat.

An AGENTS.md instruction cannot wake a session after its model turn has ended,
after sleep, or after the app closes. A background shell polling process alone
also cannot cause the model to consume its output in a new turn. Therefore the
pilot must verify that the Desktop host permits repeated awaited CLI calls and
remains active across idle periods. If it ends the turn, orchd retains queued
work and shows `offline`/`needs Operator resume`; Operator resumes this same
session with a poll instruction. Do not describe this as unattended wake-up.
Poll every 30 seconds only while an active turn exists; avoid a tight loop of
immediate empty reads. Any host-imposed tool timeout must exceed the bounded
CLI wait. MCP long polling has the same active-turn limitation.

True push would require a supported Desktop host notification that starts or
steers the registered session. No such hook was validated here. It is optional
future work, with queue event IDs as the source of truth; OS banners or thread
deep links are visibility aids and do not count as message delivery. Start with
pull plus explicit offline diagnostics rather than depending on push.

### Proposed managed AGENTS.md instructions (design only)

After backend approval, add a new managed-rule version and migration with this
loop; do not ship commands in the installed template before they exist:

```text
On Operator start/resume, verify the configured aiagent worker identity and
state location, then register/reattach this same Desktop session.
While idle in an active turn, call desktop-worker poll with a bounded wait;
on an empty timeout, call it again without ending the turn.
For a claimed task: persist its delivery receipt, acknowledge it, read its
allowed apps/sites and completion criteria, then execute only that task.
Use orchd ack/progress/ask/report with its explicit task ID and worker claim.
After ask, stop task actions and poll only for its answer or cancellation;
do not take another task. Only the matching explicit Operator answer grants
approval for the exact preview. A changed action needs a new ask.
After report is accepted, release the claim and return to idle polling.
On invalid identity, lost lease, cancellation, or uncertain prior action:
stop GUI actions, report/reconcile, and wait for Operator direction.
```

The new backend-specific waiting rule must replace today's instruction to end
the turn after `ask`: keep a bounded CLI wait active instead, so answers can be
pulled into this same turn. The Phase 1 template keeps existing worker rules
until this behavior is approved and implemented. Safety rules remain aiagent
only, no credential/payment entry, explicit approval, allowed apps/sites,
secret-free output and sanitized evidence in `reports/<task-id>/`.

### Minimal proposed CLI/MCP surface

These are proposed interfaces, not available commands:

| Surface | Minimum behavior |
| --- | --- |
| `desktop-worker register` | Operator provisions a dedicated worker identity/channel, home, and Desktop thread ID; the session reattaches with a generation-bound credential. Return registration and capability status without secrets. |
| `desktop-worker poll --wait 30` | Authenticated claim/lease of one eligible queued task, or delivery of an answer/cancellation for its active task. Return event ID, task ID, generation, claim token, and payload; empty timeouts are explicit. |
| `desktop-worker receipt <event-id>` | Idempotent acknowledgement after durably recording receipt in the worker home; keep unacknowledged events redeliverable. Task `ack` remains a separate lifecycle event. |
| `desktop-worker heartbeat` | Renew an active task's lease during GUI execution; poll also renews it while waiting. Detect stale or superseded generation and stop actions. |
| Existing `ack/progress/ask/report` | Reuse task-linked storage/Orch wake-up; add worker identity/claim validation for this backend and idempotent event keys. Report atomically releases the claim. |
| Existing dispatch/answer/view | Dispatch queues instead of spawning; answer durably queues an answer event instead of exec resume/app-server flush; view resolves the registered Desktop thread. |

CLI-only is sufficient for the pilot if the Desktop session can execute the
installed absolute orchd binary against the correct state database. Store the
worker credential in a permission-restricted external config file, never in the
repo, prompt, command arguments or screenshots. Registration must verify task
ownership and aiagent identity; an arbitrary caller naming a worker ID must not
be able to claim its queue. Existing commands lack this proposed Desktop claim
contract; do not imply that task IDs alone authenticate it. An optional MCP role
can expose register/reattach, poll, receipt, heartbeat and the four task lifecycle
actions through the same handlers, scoped to one registered worker and its
active task. Neither the interface role nor broad Orch dispatch powers are
needed in the Desktop session.

### Ownership, answers and recovery

Persist worker identity, Desktop thread ID, home, connection generation,
heartbeat, active task lease, and durable delivery IDs. Dispatch records the
queue entry before returning its receipt; a queued task is not `acked` until
the registered session claims and acknowledges it. Serialize work per session
and, where sessions share a physical screen, per computer. Task-local allowed
scope and evidence directories must reset between tasks.

Delivery is at least once. Persist receipts under this session home's reports
before acknowledging; reconcile task state and receipt after reconnect. This
must not automatically repeat a GUI action whose outcome is uncertain. Lease
expiry marks the session unavailable, retains task ownership for reconciliation,
and invalidates action approvals; it does not silently hand the action to a
replacement worker. Stale-generation events and answers are rejected.

Existing `core.py` already stores task-linked ack/progress/question/report and
wakes Orch. Add a Desktop branch to answer delivery: queue the answer against
its question ID, task, worker and generation; the session pulls it while waiting
and acknowledges receipt separately from consumption. Duplicate answers do not
repeat actions. Cancellation is also queued and invalidates the claim and
approvals. Without a verified push/interrupt hook, it can only be observed at
the next CLI poll/heartbeat checkpoint: require a checkpoint before every GUI
action and report this interruption latency honestly. App closure or a stopped
turn leaves durable work visible as unavailable, with an explicit Operator
resume path. Do not infer completion or automatically create another session.

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
The backend remains unimplemented pending Operator's decision. The first
validation target is an existing-session pull pilot, including proof of idle
polling continuity or an explicit manual resume requirement. Automated launch
is not a prerequisite for this design.
