# Dashboard answers and snapshot v2

Snapshot v2 adds `binding_counts`, one item per entry with its current `orch_id`.
Each item contains `counts` (all owners), `current_counts` (the bound owner), and
`historical_counts` (other owners), grouped by direction, kind, and delivery state.
These count all stored messages, including terminal rows; `sending` is shown as
`uncertain`. Existing v2 fields and the v1 contract keep their meanings.

`orchd binding --status` retains its all-owner `counts` and adds the same
`current_counts` / `historical_counts` split. Entry MCP `status` instead reports
only unread, nonterminal inbound messages for the bound owner in
`not_yet_delivered_to_orch`; delivered, skipped, cancelled, and superseded rows
are excluded. `historical_undelivered` gives the count and per-owner counts for
other owners using the same filter. Entry does not announce history on opening.

All examples use synthetic data. These commands operate on the configured orchd
store; a dashboard must use the CLI rather than writing SQLite itself. The local
dashboard server remains responsible for loopback/session/origin validation.

## Submit a reply

```sh
orchd entry-question answer --help
orchd entry-question answer 101 --entry desktop --orch-id synthetic-orch \
  --expected-version 1 --expected-state current --text 'Synthetic answer' \
  --idempotency-key synthetic-submission-1
orchd entry-question un-snooze --help
orchd entry-question un-snooze 102 --expected-version 1 --expected-state snoozed
```

`answer` requires a positive question ID and version, expected state `current`,
the expected responsible Orch ID, non-empty text and a non-empty idempotency key.
It rejects missing questions, mismatched versions/states, historical bindings,
and a key already used for another question/version. It does not guess a recipient.
The answer CLI always prints JSON (there is no `--json` flag). `answer --help`
states this output format. Its receipt
contains `message_id`, `reply_to`, `task_id`, `orch_id`, body byte count/hash,
`delivery`, `status`, and `duplicate`. It does not print the answer body.

Saving creates the same inbound `reply` as Desktop `relay(reply_to=...)`, marks
the question answered, records history and promotes the next queued question in
one write transaction. Existing entry delivery then notifies the responsible Orch
with metadata; the Orch reads the original answer through `entry_inbox`. No worker
is contacted by submission. Offline/failed notification preserves the reply and
the existing entry retry path. `sending` is exposed as `uncertain`, never as success.

An identical same-key payload returns the original reply ID and its current receipt.
Each submission key stores the SHA-256 of the exact submitted text. Reusing a key
with different text rejects with `idempotency conflict` and exit 1, without a write
or delivery. Pre-hash keys use the saved reply body hash for comparison; migration
does not invent historical submission payloads. Different keys for the same
question/version remain aliases of the original reply rather than creating another
reply; each alias retains its own submitted payload hash.
Retries still require the original version, current binding and an answered
question; a withdrawn/superseded question is rejected. A Desktop answer already
saved without a dashboard submission is not replaced by a dashboard answer.
An identical retry re-enters existing inbound delivery if the original reply is not
delivered; delivered replies are not sent again. Existing FIFO ordering and offline/
failed retry rules apply. A `sending`/uncertain receipt is never automatically resent,
as before. No retry inserts a second reply. A database save failure rolls back question, history,
promotion and submission key together. Transport and receipt failure retain the
existing delivery semantics; transmission is not worker acknowledgement.

`un-snooze` requires the matching version/state and accepts only snoozed questions.
It clears `revisit_at`, retains the note, records snoozed → queued history and puts
the question behind all already queued questions. With no current question it is
promoted through the existing FIFO path, with a second history event. New questions
join behind it. Reaching the revisit date does not restore a question automatically.

## Exact forwarding evidence

Orch MCP `answer(task_id, entry_reply_id=...)` reads the stored answer verbatim and
checks its entry/task association; passing both text and an ID is refused.
The ID persists on the worker's `answer_queued` and `answer` rows through Codex exec
resume, Claude socket delivery, app-server receipt/reconciliation and retry to a
replacement worker. Calls without the ID retain their existing behavior and have
a null link. No body comparison, task status, worker ack or report supplies a link.
This association does not introduce worker-answer deduplication or change flush
semantics: it records forwarding evidence, not an execution authorization.

## Additive store migration

- `messages.entry_reply_id INTEGER REFERENCES entry_messages(id)`, nullable.
- `entry_messages.notified_at REAL`, nullable; only a successful inbound wake writes it.
  Inbox reads do not invent or backfill notification receipts.
- `entry_messages.queue_order INTEGER`, nullable; existing queues fall back to ID
  order. Only explicit un-snooze and newly created questions receive queue positions.
- `entry_answer_submissions(entry_id, idempotency_key, question_id, version, reply_id)`
  with primary key `(entry_id, idempotency_key)` and references to entries/messages.
  Nullable `body_sha256 TEXT` is added idempotently to record each key's submitted
  payload hash; pre-hash records remain unchanged.

There are no destructive migrations, renamed columns, historical backfills or
rewritten old rows. Repeated ordinary store connections are idempotent. Snapshot
never migrates either the source or its private copy. Older databases lacking the
new columns/history table still yield null links/notification receipt, version 1 and empty history.

## Snapshot contract v2

`orchd snapshot --json` defaults to **v1**, matching #17's reader on the same DB:
current/queued and bounded answered rows; legacy `unknown`/`notified` notification
values; `worker_forwarded="unknown"`; `reply_id=null`. It adds no computed lifecycle
or receipt fields in v1. Persisted metadata columns retain #17's SELECT-all behavior.

Use `orchd snapshot --json --schema-version 2` to explicitly opt into **v2**, or
`--schema-version 1` to select the legacy contract. Both versions remain read-only.
`snapshot --help` documents the selector. All fields in the following table describe
opt-in v2 only; default callers keep their existing contract.

| Field | v2 meaning |
| --- | --- |
| entry_messages states | current, queued, snoozed, cancelled and superseded are included; answered remains bounded by `answered_limit` |
| version, supersedes, superseded_by, revisit_at, note, reason | Persisted lifecycle metadata; version defaults to 1 on an older schema |
| revisit_due | True only when a snoozed question's persisted revisit date is at/before generated_at; no write |
| history | Last 10 persisted state events per question, in chronological order; no invented events |
| replies.processing.saved / saved_at | Saved reply and its persisted creation time |
| orch_notification / orch_notified_at | pending, failed, uncertain or notified from entry delivery; missing evidence is null; notification timestamp is persisted notified_at; a delivered row without a notification receipt is null |
| orch_read_at | Persisted entry inbox read time, or null |
| worker_forwarded / worker_answer_ids | null without an exact link; pending for linked unsent queue rows; transport_accepted for linked answer/consumed queue receipt; IDs of the linked worker rows |
| worker_confirmed / worker_executed | Always unknown; no per-answer confirmation or execution evidence exists |
| worker_answers.entry_reply_id / reply_id | Persisted link (both names), or null for legacy/unlinked rows |
| pending_worker_questions | Latest unresolved worker question per open task, across all owners and independent of inbox read_at; includes saved answers awaiting delivery and missing-message diagnostics |

### Pending worker question contract

`pending_worker_questions` is additive in v2 only. `worker_questions` retains its
unread Orch triage meaning. The new collection uses message ID order, includes every
`store.OPEN` task, and never limits rows with `answered_limit` or owner bindings.
Each row has `id` (question message ID), `task_id`, `body`, `created_at`,
`task` (`repo`, `title`, `status`, `owner` = persisted `orch_thread`), `processing`
and `evidence`. `processing` is:

- `awaiting_decision`: no subsequent answer or accepted followup evidence.
- `saved_pending_delivery`: a subsequent unread `answer_queued` or queued followup
  receipt exists. Display as saved awaiting delivery, outside the decision queue.
- `insufficient_data`: task status is `question`, but no question message exists.
  `id`, `body`, and `created_at` are null; never invent question text.

A subsequent `answer`, consumed `answer_queued`, or structured followup with a
committed token/message and status `accepted` or `delivered` resolves the latest
question and removes it. A followup with status `queued` is saved awaiting delivery.
Matching transport row IDs are included in evidence. A matching consumed queue or
answer receipt resolves it after a later flush, even if the original attempt failed.
A failed followup with a committed unread queue stays saved awaiting delivery;
failure without a queue stays awaiting decision. Malformed and unstructured
followup events do not resolve it. Followup
instruction transport rows are excluded from ordinary answer evidence. A newer
question starts a new decision even if an older question was answered.

`evidence` includes `latest_question_id`, persisted `question_read_at`,
`read_at_affects_pending=false`, `reason`, and `resolutions` containing message IDs,
kinds, timestamps, read times, and computed resolution states. These are storage
and transport facts, not worker execution acknowledgement. Snapshot still performs
only SELECTs on a private copy, with no schema changes or runtime calls.

Compared with v1, `worker_forwarded` is now nullable/evidence-based rather than
always `unknown`; unknown notification values are null. Other stages do not imply
one another: notification and read evidence are displayed independently. A worker
transport receipt is never renamed acknowledgement or execution.

The [synthetic snapshot example](examples/dashboard-snapshot-v2.json) shows five
replies: saved/pending Orch, notified, read by Orch, queued for worker and transport
accepted by worker. No real store, transcript, credential or project content is used.

## Answer worker questions directly

```sh
orchd task-question answer --task TASK_ID --question-id MESSAGE_ID \
  --text-file /path/to/answer.txt --idempotency-key UNIQUE_KEY --actor dashboard
```

Use the `id` from an `awaiting_decision` row in `pending_worker_questions`.
The UTF-8 file is sent verbatim. `--actor` is `dashboard` (default) or `operator`.
Each call prints a JSON receipt with `status` (`delivered`, `queued`, `failed`),
`delivered`, `pending` (queued worker answer count), plus task/question/queued-message
IDs and actor when accepted. A failed validation exits 1 and sends nothing.

Under the shared task delivery lock, a new submission requires an open task and
its latest unanswered question. Older questions, another task's question, missing
questions and already saved/resolved questions are stale. A new key cannot answer
a closed task. Question validation, answer queue, actor event and durable receipt
are committed together in the orchestrator DB. Delivery then uses
`core.answer(..., flush=True)`, including its existing Codex, Claude and app paths.
MCP `answer` retains its current API and behavior.

The key is scoped to the task. Identical key/question/text/actor retries return the
same persisted receipt without queueing or flushing again, including after a
restart, close, or newer question. Reusing a key with different input is an
idempotency conflict. A separate cross-process submission lock serializes retries
through final receipt publication; no SQLite write transaction spans a transport
call. The existing queue and task delivery lock still serialize transports.

If a process dies after accepting the queue but before saving the transport
result, its provisional `queued` receipt remains recoverable. Retrying the key
does not attempt another send. Check current worker/queue evidence before manually
flushing after an uncertain send. Later ordinary answer/auto-flush operations can
deliver queued rows; they do not rewrite this historical submission receipt.
This guarantees submission idempotency, not exactly-once execution by the worker.

`task_question_answer` events contain the actor and receipt in the owner's inbox.
`list_open.latest_question_answer` exposes the latest accepted submission's actor,
question/answer message IDs, creation time and persisted receipt, even after the
inbox is read. This command adds a `task_question_answers` receipt table via the
existing store bootstrap; deployment/migration of a real store is outside this task.

## Remaining follow-ups

This implements #18 review's un-snooze and superseding queue-count correction.
Compact status/inbox lifecycle enrichment, entry-lock wait behavior, lifecycle
constant cleanup, concurrent two-Orch MCP cancellation coverage and other review
notes remain separate work. The dashboard HTTP/UI, live migration, package upgrade,
daemon restart, deployment and worker execution acknowledgement are outside this change.
