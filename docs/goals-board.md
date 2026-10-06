# Central goals and the Orch board

Decisions: [framework](https://github.com/NatChung/orchd/issues/76#issuecomment-5978844086),
[PG/V inventory](https://github.com/NatChung/orchd/issues/76#issuecomment-6007662310),
[grill summary, 2026-10-06](https://github.com/NatChung/orchd/issues/76#issuecomment-6008941333).

Goal data lives only in the central `ORCHD_HOME/orchd.db`. Repo names identify projects;
companies are descriptive labels, never project keys. This is opt-in: old tasks keep null
`goal_id` and `goal_critical`, and appear in Later. No daemon, hook, auto-refresh or Artifact
publishing is added. Markdown export is a snapshot for the Orch home, not a repo source of truth.

## Commands

```sh
orchd goal add orchd --type continuous --pg '預算 ≤20%＋backlog' --v 5 --actor Nat \
  --source 'https://github.com/NatChung/orchd/issues/76#issuecomment-6007662310'
orchd goal list --repo orchd
orchd goal set GOAL_ID --sprint-goal '交付看板 PR' --sprint-start 2026-10-05 --sprint-end 2026-10-11 \
  --done-when 'PR 與完整測試證據可供審閱' --authority Nat --ball Orch
orchd goal set GOAL_ID --fields '{"deadline":null,"v":null}'
orchd goal show GOAL_ID
orchd goal export --md > ~/orch/home/groups/goals.md
orchd board --html ~/orch/home/groups/board.html
```

`add` and `set` accept flags for all fields, or a JSON object via `--fields`. Flag values take
precedence. Dates are `YYYY-MM-DD`; optional dates and V can be cleared with JSON null.
`companies` is a JSON array, e.g. `--companies '["KC"]'`. Repo is immutable after creation.
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
Record V only from Nat's approved value; the tool does not compute business value or enforce Nat's
identity. Preserve provisional/source notes in `source`, and do not invent confirmation dates.

## Score and view semantics

`score = (V + T + S + B) / J`, weights all 1, V/T/S/B clamped to 0–10; J in 1/2/3/5/8.
The day-to-score mapping below is the initial implementation convention, subject to the two-week
calibration on **2026-10-20**, not an additional Nat-approved business rule:

- V: Nat's recorded value; null remains unknown and produces no numeric score.
- T: `clamp(10 - calendar days until deadline, 0, 10)`; use explicit deadline, then sprint end;
  no deadline gives 0. Overdue dates give 10.
- S: elapsed calendar days since the latest explicit `last_progress_date` or linked task's
  progress/report message, capped at 10. Creating a goal defaults to today's date; edit/import
  can supply an older date or null. Ordinary metadata edits do not reset progress.
- B: elapsed calendar days since `waiting_nat_since` when `ball=Nat`, or the oldest pending
  current/queued question addressed to Nat for the goal; capped at 10. Other waiting does not count.

Project score is the highest known active/waiting goal score. Paused projects follow others;
unknown-score projects sort after known ones; ties sort by repo name. Done/paused goals do not
raise priority. Sprint Goal and goal-critical needs-Nat come first, PG stays small. Blocked tasks
are expanded. Noncritical records, secondary Nat questions, Later, done items and paused projects
start collapsed. Worker `done` is **已回報待核對**, distinct from goal status `done` and acceptance.
Needs-Nat comes from pending outgoing `entry_messages` questions, including queued/held questions;
workers' questions to Orch are task statuses, not automatically Nat questions. Questions without
an open linked task appear under `unassigned` in the secondary queue, without invented project links.

Read CLI operations (`show/list/export/board`) copy the DB and WAL using the existing stats snapshot
mechanism and migrate only the private copy. They do not consume inbox, deliver questions, change
task status, or write beside the source DB. Board HTML is self-contained with keyboard accessible
tabs, native details, light/dark v1 palette and system fonts; opening it sends no external requests.
Re-run the board command to refresh. There are no mutation buttons, external scripts or fonts.

## Import the #76 PG table after merge

**Not run by this implementation.** Nat/Orch should first confirm the repo-name mapping and review
each row, then use `goal add` against the intended central `ORCHD_HOME`. Do not store exported goal
records in product repos. The table below preserves uncertainty from the source; it does not fill
missing PGs or promote provisional V to confirmed decisions.

| Repo name to use | type | PG to import | V | status / source note |
|---|---|---|---|---|
| Actual CHASR repo name, confirm before import | goal | PG 具體內容待補入紀錄 | 9 | active; 推進 |
| pohai-hub | goal | App 交付與上架；10 月中結案、10 月底收款 | 8 | active; 推進 |
| kc-storefront | goal | agentic-engineering 交付模式切換；第 5–6 週 Sprint Goals 待擬 | 7 | active; rollout plan line 330 |
| Actual VPIN repo name, confirm before import | goal | PG 具體內容待補入紀錄 | 8 | active; 推進 |
| hwa-guan-hub | goal | 10 月底三平台測試版；年底完整替換 | 7 | active; V 暫定 |
| dek-hub | goal | 10 月中部署／打包交付、10 月底收款 | 6 | waiting; V 暫定; ball 客戶; blocker AWS 帳號; follow-up 2026-10-07 |
| yite-hub | goal | 暫停 | 5 | paused |
| kc-domain-graph | continuous | 預算＋backlog | 6 | active; V 提案 |
| orchd | continuous | 預算 ≤20%＋backlog | 5 | active; V 暫定 |
| nat-assistant | continuous | 預算＋backlog | null | active; V 未定 |

Use source `https://github.com/NatChung/orchd/issues/76#issuecomment-6007662310` plus each row's
provisional notes. `last_confirmed_date` stays null until the goal itself is confirmed. Do not
invent sprint windows, deadlines, evidence or authority. Example for the customer wait:

```sh
orchd goal add dek-hub --pg '10 月中部署／打包交付、10 月底收款' --v 6 \
  --status waiting --ball 客戶 --blocker 'AWS 帳號' --follow-up-date 2026-10-07 \
  --source 'https://github.com/NatChung/orchd/issues/76#issuecomment-6007662310; V 暫定'
```

Review `goal list` before adding: import is manual, not idempotent, and repeated adds create new
records. Update an existing id instead when it represents the same outcome. KC goals are central
too; kc-log links can be added to `source` later without moving storage.
