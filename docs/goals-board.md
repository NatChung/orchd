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
`goal_set({goal_id?, fields})`. Omit `goal_id` and provide `fields.repo` to create.
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

