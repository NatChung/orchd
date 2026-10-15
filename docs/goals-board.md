Goal data lives only in the central `ORCHD_HOME/orchd.db`. Repo names identify projects;
companies are descriptive labels, never project keys. This is opt-in: old tasks keep null
`goal_id` and `goal_critical`, and appear in Later. No daemon, hook, auto-refresh or Artifact
publishing is added. Markdown export is a snapshot for the Orch home, not a repo source of truth.

## Commands

```sh
orchd goal add orchd --type continuous --pg '預算 ≤20%＋backlog' --v 5 --actor Operator \
  --source 'example-source'
orchd goal list --repo orchd
orchd goal set GOAL_ID --sprint-goal '交付看板 PR' --sprint-start 2026-10-05 --sprint-end 2026-10-11 \
  --done-when 'PR 與完整測試證據可供審閱' --authority Operator --ball Orch
orchd goal set GOAL_ID --fields '{"deadline":null,"v":null}'
orchd goal show GOAL_ID
orchd goal export --md > ~/orch/home/groups/goals.md
orchd board --html ~/orch/home/groups/board.html
```

`add` and `set` accept flags for all fields, or a JSON object via `--fields`. Flag values take
precedence. Dates are `YYYY-MM-DD`; optional dates and V can be cleared with JSON null.
`companies` is a JSON array, e.g. `--companies '["ExampleCompany"]'`. Repo is immutable after creation.
Multiple goal records per project support separate sprint outcomes; set old goals to done/paused.
`goal show` includes all linked tasks and the complete history. Every actual goal mutation writes
actor, timestamp and field before/after values in the same transaction; no-op saves add nothing.
CLI defaults to `operator:<OS user>`; `--actor` is an audit attribution, not authentication.

Attach existing tasks with an explicit replacement list (omit to preserve existing links):

```sh
orchd goal set GOAL_ID --linked-tasks '[{"task_id":"TASK_ID","goal_critical":true}]'
orchd goal set GOAL_ID --linked-tasks '[]'
```

Tasks must belong to the same repo. A task cannot silently move from another goal: remove the old
link first. Linking leaves worker status and timestamps alone. Dispatch can optionally pass
`goal_id` and `goal_critical`; the default remains null. Linked dispatches also record goal history.

Orch MCP tools: `goal_list({repo?, status?})`,
`goal_set({goal_id?, expected_revision?, fields})`. Omit `goal_id` and provide `fields.repo` to create.
The history actor comes from the caller's Orch id. Entry-role MCP cannot call these tools.
Record V only from Operator's approved value; the tool does not compute business value or enforce Operator's
identity. Preserve provisional/source notes in `source`, and do not invent confirmation dates.

## Score and view semantics

`score = (V + T + S + B) / J`, weights all 1, V/T/S/B clamped to 0–10; J in 1/2/3/5/8.
The day-to-score mapping below is the initial implementation convention, subject to the two-week
calibration on **2026-10-20**, not an additional Operator-approved business rule:

- V: Operator's recorded value; null remains unknown and produces no numeric score.
- T: `clamp(10 - calendar days until deadline, 0, 10)`; use explicit deadline, then sprint end;
  no deadline gives 0. Overdue dates give 10.
- S: elapsed calendar days since the latest explicit `last_progress_date` or linked task's
  progress/report message, capped at 10. Creating a goal defaults to today's date; edit/import
  can supply an older date or null. Ordinary metadata edits do not reset progress.
- B: elapsed calendar days since `waiting_nat_since` when `ball=Operator`, or the oldest pending
  current/queued question addressed to Operator for the goal; capped at 10. Other waiting does not count.

Project score is the highest known active/waiting goal score. Paused projects follow others;
unknown-score projects sort after known ones; ties sort by repo name. Done/paused goals do not
raise priority. Sprint Goal and goal-critical needs-Operator come first, PG stays small. Blocked tasks
are expanded. Noncritical records, secondary Operator questions, Later, done items and paused projects
start collapsed. Worker `done` is **已回報待核對**, distinct from goal status `done` and acceptance.
Needs-Operator comes from pending outgoing `entry_messages` questions, including queued/held questions;
workers' questions to Orch are task statuses, not automatically Operator questions. Questions without
an open linked task appear under `unassigned` in the secondary queue, without invented project links.

Read CLI operations (`show/list/export/board`) copy the DB and WAL using the existing stats snapshot
mechanism and migrate only the private copy. They do not consume inbox, deliver questions, change
task status, or write beside the source DB. Board HTML is self-contained with keyboard accessible
tabs, native details, light/dark v1 palette and system fonts; opening it sends no external requests.
Re-run the board command to refresh. There are no mutation buttons, external scripts or fonts.

## Ordered issue scope (Refs #48)

`goal_issues` is the authority for the ordered issues belonging to the goal's current
`sprint_goal`. This is the explicitly approved exception to the earlier dashboard
no-schema-change constraint. Only this table and its index are added. GitHub/Jira
continue to own issue content and state. Task dispatch, entry tags and intake links
keep their existing meanings; none automatically populate or replace scope.
Changing `sprint_goal` text preserves scope unless `issues` is also supplied.

Orch records Operator-confirmed scope with the existing `goal_set` tool:

```json
{"goal_id":"GOAL_ID","expected_revision":12,"fields":{"issues":["github:example/project#3","jira:DEMO-2","github:example/project#1"]}}
```

Read `goal_list` first and use that goal's `issue_scope.revision`. Attach by adding a
ref to the ordered array, remove by omitting it, and reorder by sending the desired
array. This is an entire replacement, not an append. Equivalent CLI:

```sh
orchd goal set GOAL_ID --expected-revision 12 --issues '["github:example/project#3","jira:DEMO-2"]'
orchd goal set GOAL_ID --expected-revision 13 --issues '[]'
```

- Omitted `issues` preserves scope; `[]` explicitly confirms an empty scope. Null,
  malformed refs and duplicate canonical refs are rejected before any writes.
- Refs use `github:owner/repo#N` or `jira:KEY-N`, with positive issue numbers.
  GitHub owner/repo is lowercased; Jira keys must already be uppercase. No network
  lookup is required. Legal refs remain `resolution: "unresolved"` until the reader
  joins tracker data; this says nothing about issue status or access permission.
- Same-goal repeats do not create duplicates. The same canonical issue may belong
  to multiple goals, each with its own order. Removing soft-deletes membership;
  readding clears `removed_at` on the same primary key and preserves `created_at`.
- Writers assign contiguous positions starting at zero. Readers sort by position,
  then canonical ref. Positions are not UNIQUE, so swaps need no temporary ranks.
- Scope, goal metadata, `updated_at`, and `goal_history` before/after, actor and
  timestamp commit together. First explicit empty scope records `before: null`,
  `after: []`. A later identical write leaves timestamps and history unchanged.
- Existing goals require an integer `expected_revision` when `issues` is supplied.
  It is the latest goal_history id (zero for legacy goals without history).
  All goal changes, including task links, advance it. Stale or missing revisions
  fail atomically; reread and reconcile. Repeating an identical list with the
  current revision is a no-op; an old revision is still rejected. Creation needs
  no expected revision. Actor is audit attribution, not authorization.

For dashboard reads use `orchd snapshot --json --schema-version 2`, which never
migrates either DB. See [the readonly contract](dashboard-answers.md#ordered-goal-issues-v2-addition).
The older goal show/list/export convenience commands migrate their private copy;
they are not a capability probe for the source database.

## Controlled database upgrade

`store.connect(explicit_path)` creates the empty table and index in the existing
`BEGIN IMMEDIATE` schema transaction. All DDL/column additions commit together;
any failure rolls back and closes the connection, and reopening retries safely.
There is no backfill or change to existing table definitions, no new migration
framework, and no change to global foreign-key enforcement. Writers verify the
goal exists even when SQLite foreign keys are disabled.

Deploy separately after review: pause planning writes, make a consistent SQLite
backup using the backup API (or stop all writers before copying), verify restore,
install the approved commit, then explicitly upgrade the intended DB and verify
schema, old rows and repeatability. Home migrations are a separate mechanism.
Restart the relevant Orch/MCP processes in an approved window to load the new
code/tool schema. Only then initialize Operator-confirmed scope. Installation,
DB upgrade, process restart and scope initialization are separate acceptance steps.
Rollback can retain the additive table; restoring a backup requires stopping
writers and assessing events written since backup. This implementation does not
perform installation, production migration, restart or real scope initialization.
