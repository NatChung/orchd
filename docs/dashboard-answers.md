# Dashboard answers and snapshot v2

All examples use synthetic data. These commands operate on the configured orchd
store; a dashboard must use the CLI rather than writing SQLite itself. The local
dashboard server remains responsible for loopback/session/origin validation.

## Submit a reply

```sh
orchd entry-question answer --help
orchd entry-question answer 101 --entry desktop --orch-id synthetic-orch \
  --expected-version 1 --expected-state current --text 'Synthetic answer' \
  --idempotency-key synthetic-submission-1 --json
orchd entry-question un-snooze --help
orchd entry-question un-snooze 102 --expected-version 1 --expected-state snoozed
```

`answer` requires a positive question ID and version, expected state `current`,
the expected responsible Orch ID, non-empty text and a non-empty idempotency key.
It rejects missing questions, mismatched versions/states, historical bindings,
and a key already used for another question/version. It does not guess a recipient.
The CLI returns JSON by default; `--json` makes that contract explicit. Its receipt
contains `message_id`, `reply_to`, `task_id`, `orch_id`, body byte count/hash,
`delivery`, `status`, and `duplicate`. It does not print the answer body.

Saving creates the same inbound `reply` as Desktop `relay(reply_to=...)`, marks
the question answered, records history and promotes the next queued question in
one write transaction. Existing entry delivery then notifies the responsible Orch
with metadata; the Orch reads the original answer through `entry_inbox`. No worker
is contacted by submission. Offline/failed notification preserves the reply and
the existing entry retry path. `sending` is exposed as `uncertain`, never as success.

The same key or question/version returns the original reply ID and its current
receipt without saving or notifying again, even if retried with different text.
New keys for that same question/version are retained as aliases of that receipt.
Retries still require the original version, current binding and an answered
question; a withdrawn/superseded question is rejected. A Desktop answer already
saved without a dashboard submission is not replaced by a dashboard answer.
Retrying submission does not retry notification: use the existing entry status/
delivery recovery path. A database save failure rolls back question, history,
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

There are no destructive migrations, renamed columns, historical backfills or
rewritten old rows. Repeated ordinary store connections are idempotent. Snapshot
never migrates either the source or its private copy. Older databases lacking the
new columns/history table still yield null links/notification receipt, version 1 and empty history.

## Snapshot contract v2

`orchd snapshot --json` keeps the v1 top-level arrays and read-only behavior and
changes `schema_version` to **2**. Consumers must recognize the new version.

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

Compared with v1, `worker_forwarded` is now nullable/evidence-based rather than
always `unknown`; unknown notification values are null. Other stages do not imply
one another: notification and read evidence are displayed independently. A worker
transport receipt is never renamed acknowledgement or execution.

The [synthetic snapshot example](examples/dashboard-snapshot-v2.json) shows five
replies: saved/pending Orch, notified, read by Orch, queued for worker and transport
accepted by worker. No real store, transcript, credential or project content is used.

## Remaining follow-ups

This implements #18 review's un-snooze and superseding queue-count correction.
Compact status/inbox lifecycle enrichment, entry-lock wait behavior, lifecycle
constant cleanup, concurrent two-Orch MCP cancellation coverage and other review
notes remain separate work. The dashboard HTTP/UI, live migration, package upgrade,
daemon restart, deployment and worker execution acknowledgement are outside this change.
