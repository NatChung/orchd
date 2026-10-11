# Entry question lifecycle

Existing `ask_nat → relay(reply_to) → answered → promote` behavior stays in place.
Lifecycle operations are explicit and require the question ID, its expected version,
and its expected state. A mismatch rejects the operation without changing the question,
queue or history. Operations apply only to the current entry binding.

| Operation | Allowed old states | Result |
| --- | --- | --- |
| snooze | current, queued, snoozed | snoozed, required `revisit_at`, optional `note` |
| cancel | current, queued, snoozed | cancelled, required `reason` |
| supersede | current, queued, snoozed, cancelled, answered | superseded with `superseded_by`; new question has `supersedes` and version + 1 |

When a current question leaves that state, the earliest queued question becomes current
and uses the existing delivery path. A replacement joins the end of that queue; with no
queued/current question it becomes current itself. Revisions have new IDs and do not
inherit replies or task links: pass a task ID explicitly for the new question.
Replies to snoozed, cancelled or superseded questions are refused. A stored reply to an
answered question that was subsequently superseded cannot be forwarded through
`answer(entry_reply_id=...)`; obtain a new answer for the revision.

`revisit_at` is a finite, non-negative Unix timestamp. A past date is allowed.
`entry-question show` returns `revisit_due` for a snoozed question whose date has arrived;
reading or reaching that date never restores, cancels or answers the question.
There is no automatic expiry, cancellation or response. Reopen a cancelled or snoozed
question by creating a linked revision with `supersedes`.

## CLI

```sh
orchd entry-question --help
orchd entry-question show --help
orchd entry-question snooze --help
orchd entry-question cancel --help
orchd entry-question supersede --help

orchd entry-question show 101 --entry desktop
orchd entry-question snooze 101 --expected-version 1 --expected-state current --revisit-at 1900000000 --note 'Review later'
orchd entry-question cancel 101 --expected-version 1 --expected-state snoozed --reason 'No longer needed'
orchd entry-question supersede 101 --expected-version 1 --expected-state cancelled --reason 'Revised scope' --text 'Revised question?'
```

All examples are synthetic. `show` includes metadata, body and history; it may inspect
questions from a previous binding, but lifecycle writes reject them.
The CLI records the actor as `operator:<local user>`.

## Orch MCP

- `entry_question_show(question_id)` reads lifecycle metadata/history on the bound entry.
- `entry_question_snooze(question_id, expected_version, expected_state, revisit_at, note?)`.
- `entry_question_cancel(question_id, expected_version, expected_state, reason)`.
- `ask_nat(text?, task_id?, quote_worker_question?, supersedes?, expected_version?, expected_state?, reason?)`.
  When `supersedes` is supplied, both expectations and a non-empty reason are required.
  Without `supersedes`, those three arguments must be omitted; existing calls keep working.

MCP writes derive the actor from the bound caller (`orch:<id>`), rather than a supplied
identity. Entry-role MCP cannot call these Orch tools. Existing binding summaries keep
their original compact ID/task shape; use `entry_question_show` for expectations.
Question results from `ask_nat` and entry status include the version and revision metadata.

## Additive schema and history

Only six columns are added to `entry_messages`:

- `version INTEGER NOT NULL DEFAULT 1`.
- `supersedes INTEGER REFERENCES entry_messages(id)`, nullable, default NULL.
- `superseded_by INTEGER REFERENCES entry_messages(id)`, nullable, default NULL.
- `revisit_at REAL`, `note TEXT`, `reason TEXT`, nullable, default NULL.

The new `entry_question_history` table contains `id`, `question_id`, `version` (default 1),
`from_state` (nullable for creation), `to_state`, `actor`, `reason` (default empty text),
and `created_at`; an index orders each question's history. Creation and every subsequent
state transition record the actor, timestamp, version and reason in the same transaction.
Existing rows are not rewritten or backfilled with invented historical events. Migration
is idempotent and preserves every original column value; it runs only when the ordinary
store connection is used, as before.

Snoozed, cancelled and superseded questions retain their transport receipts. Delivery retry and entry status
exclude them, so a previous pending/failed delivery cannot reintroduce a withdrawn question.
Already delivered Desktop text is not retracted; stale reply IDs are rejected in the store.

This change does not add dashboard answer submission, answer receipts, automatic responses,
or snapshot JSON integration. The snapshot extension is deferred until its separate
read-only contract is merged and this branch can be rebased onto it.
