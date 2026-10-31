# orchd command reference

[繁體中文](commands.zh-TW.md)

Based on argparse in `orchd/cli.py` and dynamic fields in `orchd/goals.py`. All commands, subcommands, positional arguments and flags are covered below. Every level supports `-h` / `--help`.

## Remember these for daily use

| Command | Purpose |
| --- | --- |
| `orchd orch restart` | Replace the Desktop-bound Orch, adopt open tasks and rebind. |
| `orchd list` | Check open tasks and health. |
| `orchd doctor` | Check machine readiness. |
| `orchd upgrade` | Update the uv-installed program and managed home rules. |

## BREAKING CHANGE: upgrade commands

Orch commands now require an explicit `orch start|stop|restart|list|attach` action. The old entry points were removed; there are no aliases. Update scripts, shell aliases, runbooks and handoff notes using this table. Existing flags and action behavior stay the same.

| Before upgrade (removed) | After upgrade |
| --- | --- |
| `orchd orch [--model ...] [--no-attach]` | `orchd orch start [--model ...] [--no-attach]` |
| `orchd orch-stop ID` | `orchd orch stop ID` |
| `orchd orch-restart [ID]` | `orchd orch restart [ID]` |
| `orchd orchs` | `orchd orch list` |
| `orchd attach ID` | `orchd orch attach ID` |

Removed entry points exit with code 2 and point to the new command. Without a subcommand, the Orch command prints its action help and exits with code 2; it does not start an Orch. `--help` still exits with code 0.

## Installation and maintenance

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd init [--from OLD_HOME] [--no-trust]` | Create home/interface templates and configure trust without overwriting edited templates. | `orchd init` |
| `orchd doctor [--profile example] [--json] [--no-input]` | Check machine readiness; on the first terminal run, offer to save the projects directory. `--json` / `--no-input` never prompt. | `orchd doctor` |
| `orchd upgrade` | Update from the recorded source, then migrate home rules; `--dry-run` previews only, `--migrations-only` retries without reinstalling. | `orchd upgrade` |

`init --from` copies content from an old Orch home. It replaces destination files that still match an unmodified init template; other existing files are kept and listed. It skips `.git`, `.gitignore`, `.DS_Store`, `.claude`, `.codex`, and `AGENTS.md`; `--no-trust` skips Codex trust writes and Claude trust checks. Doctor exit codes: 0 passed, 1 required check failed, 2 required check unknown. `--profile example` is an optional fictional layout.

## Orch management

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd orch start [--model sonnet\|opus] [--no-attach]` | Start a Claude Orch and attach by default. | `orchd orch start --model sonnet` |
| `orchd orch stop ORCH_ID` | Stop the named Claude Orch. | `orchd orch stop ORCH_ID` |
| `orchd orch restart [OLD_ID] [--model sonnet\|opus] [--dry-run]` | Replace an Orch, adopt its open tasks and rebind its Desktop entry. | `orchd orch restart` |
| `orchd orch list [--all] [--json] [--restore ORCH_ID]` | List live/resumable Orchs; restore clears archival and resets death observation. | `orchd orch list` |
| `orchd orch attach ORCH_ID [--viewer]` | Attach to an existing Orch; resume its conversation if idle. | `orchd orch attach ORCH_ID` |
| `orchd adopt NEW_ORCH (TASK_ID ... [--from OLD_ORCH] \| --from OLD_ORCH) [--force]` | Operator: transfer open tasks to another Orch. | `orchd adopt NEW_ORCH --from OLD_ORCH` |

`orch start` defaults to opus; `--no-attach` only starts it. `orch list` hides dead, unknown and archived Orchs by default; `--all` includes them and `--json` emits the complete inventory. `orch attach --viewer` opens Ghostty. After `NEW_ORCH`, provide at least one `TASK_ID` or `--from OLD_ORCH`; both may be used together. `adopt --from` transfers all open tasks or limits named task IDs; `--force` also permits transfer from an alive/unknown owner and notifies it.

## Desktop entry

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd binding [--entry NAME] [--new \| --to ORCH_ID \| --status]` | Bind the Desktop interface to a live Claude Orch, or inspect the binding. | `orchd binding` |

Run `orchd init` first to create the interface configuration. `binding` defaults to entry desktop. It starts an Orch when there is no binding yet; otherwise it reuses the binding. A stopped or confirmed-dead bound Orch causes an error: use `--new` explicitly to start and rebind, leaving the old Orch's open questions with it. A session retired by Claude for idling can resume in place; a failed resume also requires `--new`. Mutually exclusive modes: `--new` starts a new Orch, `--to` selects a live Orch, `--status` reads binding/health/questions/delivery only.

## Tasks and goals

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd list` | Show open tasks, worker/Orch health and unread delivery failures. | `orchd list` |
| `orchd watch [--since HH:MM]` | Watch the live Orch/worker message timeline. | `orchd watch` |
| `orchd summary [--since HH:MM]` | Summarize workers, models, questions, parallelism and tokens per Orch. | `orchd summary` |
| `orchd board --html PATH` | Write a private static HTML snapshot without consuming inbox. | `orchd board --html /tmp/orchd-board.html` |
| `orchd snapshot --json [--schema-version 1|2] [--answered-limit N]` | Read dashboard JSON from a private DB/WAL copy without migrations or inbox consumption. | `orchd snapshot --json --answered-limit 100` |
| `orchd task-question answer --task ID --question-id ID --text-file PATH --idempotency-key KEY [--actor dashboard\|operator]` | Answer the latest pending worker question with durable idempotency; prints a JSON receipt. | `orchd task-question answer --task abc --question-id 42 --text-file /tmp/answer.txt --idempotency-key dashboard-42` |
| `orchd task-question dismiss --task ID --question-id ID --reason TEXT --idempotency-key KEY [--actor dashboard\|operator]` | Dismiss an expired decision with a durable audit and Orch inbox event; sends nothing to the worker. | `orchd task-question dismiss --task abc --question-id 42 --reason expired --idempotency-key dismiss-42` |
| `orchd task link --task ID --ref REF [--actor orch\|operator] [--idempotency-key K]` | Set the parent ref with an event superseding the previous link; prints JSON, no tracker writes. | `orchd task link --task example-task --ref jira:DEMO-7` |
| `orchd task unlink --task ID [--actor orch\|operator] [--idempotency-key K]` | Clear the current ref while preserving history; no task or tracker status change. | `orchd task unlink --task example-task` |
| `orchd goal add\|set\|show\|list\|export` | Manage central goals through the five subcommands below. | `orchd goal list` |
| `orchd goal add REPO [GOAL_OPTIONS]` | Create a goal for a repository and record its audit actor. | `orchd goal add example --type goal --intent "Ship a reviewed change"` |
| `orchd goal set GOAL_ID [GOAL_OPTIONS]` | Update a goal and its audit history. | `orchd goal set GOAL_ID --status waiting --ball Operator` |
| `orchd goal show GOAL_ID` | Read a goal, linked tasks and complete history. | `orchd goal show GOAL_ID` |
| `orchd goal list [--repo REPO] [--status active\|waiting\|paused\|done]` | Read goals filtered by repository or status. | `orchd goal list --repo example` |
| `orchd goal export --md [--repo REPO]` | Print a Markdown snapshot of central goals. | `orchd goal export --md` |

Both `--since` flags use local HH:MM today. `watch` starts after the last message ID whose timestamp is before the cutoff, then follows new messages; it defaults to the last 30 minutes. `summary` selects tasks created at or after the cutoff and summarizes all messages belonging to those tasks; it defaults to all tasks. Board writes a file; rerun to refresh it.

`snapshot` requires `--json`. It includes goals/history, open tasks, entry questions/reply metadata and separate worker question/answer evidence. `--answered-limit` is non-negative and defaults to 100; it limits only recent answered entry questions, while current/queued questions (including held) remain unlimited. Historical bindings and closed task links stay visible. Worker questions await Orch triage; worker confirmation/execution remains unknown. The output includes `generated_at`, `source_db`, output contract `schema_version` and actual database `schema_fingerprint`. Missing, corrupt or incomplete databases produce an error and a non-zero exit code.

### Ordered goal scope

`goal add/set --issues '["github:example/project#1","jira:DEMO-2"]'` replaces the
ordered issue scope; omit to preserve, `[]` to clear. Existing goals require
`--expected-revision N` from `issue_scope.revision` returned by goal_list/show.
MCP uses `goal_set({goal_id, expected_revision, fields: {issues: [...]}})`.
Dashboard reads use `snapshot --json --schema-version 2`; the additive capability
distinguishes unsupported, unset and explicitly empty scope without migration.
See [goal scope and upgrade contract](goals-board.md#ordered-issue-scope-refs-48).

### Parent references: CLI, MCP and snapshot v2

These behaviors match main `9bfb35a`. REF is `github:owner/repo#N` or
`jira:KEY-N`, with positive N. Validation checks format only, without checking
tracker existence or access. Both CLI actions default actor to orch; actor is
audit attribution. Keys are task-scoped and generated/returned when omitted.
Replaying the same key and payload returns the original receipt without restoring
a superseded ref; reusing a key with a different ref or actor fails. JSON receipts
include task_id, issue_ref, changed, actor, created_at, message_id, supersedes,
idempotency_key and status. Failures print JSON and exit 1.

Links use last-writer-wins supersession, preserving history rather than rejecting
replacement. There is no expected-ref flag or conditional update on the old value.
Orch maintains refs and must confirm the current ref before changing it.

MCP tools (there is no shell dispatch command):

- `dispatch` accepts optional `issue_ref`; omission leaves the task unlinked.
- `dispatch` without `model` uses the configured worker default (factory `sonnet`, Claude Sonnet 5.5). Worker aliases form a tier ladder defined by the built-in catalog plus optional `[models.*]` in `~/.config/orchd/config.toml` (`orchd config show` prints the effective table): tier 1 `terra` (GPT-6.1 Terra, `gpt-6.1-terra`, Codex) and `sonnet`, which `auto` chooses between by remaining quota; tier 2 `sol` (GPT-6.1 Sol); tier 3 `fable` (Claude Fable 5.1) and `astra` (GPT-6 Astra, `gpt-6-astra`). `retry` with `model="escalate"` moves one tier up (other vendor first); review pairs the other vendor at the same tier (terra↔sonnet, sol→sonnet, fable↔astra), and that pairing applies whatever the reviewer's tier; only `auto` is limited to tier 1. Example override: `[models.terra]` + `id = "gpt-6.2-terra"`; fields are `id`, `vendor` (codex|claude), `quota` (codex|claude: the quota bucket the alias draws on; defaults to its vendor), `tier`. A new process reads the file; no code change or reinstall. `[interface]` `model`/`effort` set what `orchd init` writes into the Desktop interface's `.codex/config.toml` (factory `gpt-6.1-terra`, `low`). Quota providers keep their stored keys: `sol` is the Codex bucket (`limit_id` codex), `sonnet` the Claude bucket. Terra, Astra and Fable have no price in `stats` (unpriced, not guessed).
  See [Worker model setting](#worker-model-setting). `retry` still requires `model`. `backend=app-server` needs a Codex model,
  so explicitly requesting app-server while the effective model is `sonnet` is rejected; pass `model=sol`.
  Omitted backend is resolved after model: new `sol` uses app-server, `sonnet` keeps Claude. Explicit `backend=exec` preserves the Codex exec path; existing attempts are unchanged.
  Other required arguments retain their existing rules.
- `task_link` requires `task_id` and `ref`, with optional `idempotency_key`.
- `task_unlink` requires `task_id`, with optional `idempotency_key`.

Both link tools require a caller thread id and use actor orch. They do not check
whether that Orch owns the task, like close and followup; Orch must verify the task.
Dispatch spawns the worker before writing the link. A link failure after spawn
returns an error, but the task/worker may already run without a link. Use list_open
or snapshot to identify the created task and current ref, then repair with
task_link or CLI link instead of blindly dispatching another worker.

`orchd snapshot --json --schema-version 2` includes `issue_ref` on top-level and
embedded tasks (null when unlinked), plus
`issue_links: {version: 1, available: true, history: [...]}`. History retains link,
unlink and superseded audit events. v1 omits these additions. Snapshot stays
read-only, without migrations or tracker calls. Missing fields in older versions
do not prove that tasks have no parent.

For older MCP dispatch schemas without issue_ref, record the formal parent in
instructions and report the unavailable dependency. After upgrading, confirm
and add the formal link. See [issue-linked workflow](issue-linked-workflow.md).

### GOAL_OPTIONS: every add/set flag

`--actor ACTOR`, `--fields JSON`, and the following field flags (both subcommands support all of them):

```text
--type --intent --pg --sprint-goal
--done-when --evidence --authority --status
--ball --blocker --source --plan
--sprint-start --sprint-end --follow-up-date --last-confirmed-date
--deadline --last-progress-date --waiting-nat-since --companies
--v --j --linked-tasks
```

Every field flag takes a value. General fields are text; dates use YYYY-MM-DD. `--companies` takes a JSON string array; `--linked-tasks` takes a JSON array of objects with `task_id` and boolean `goal_critical`. `--v` is a number from 0–10 recording Operator-approved value; `--j` is 1, 2, 3, 5 or 8. `--type` is goal/continuous; `--status` is active/waiting/paused/done. `--fields` is a JSON object with underscore field names; flags override it. To clear optional dates or v, pass JSON null through `--fields`, for example `orchd goal set GOAL_ID --fields '{"deadline": null, "v": null}'`. `--deadline null` and `--v null` do not work; do not override the null with a flag for the same field. Repo is immutable after creation. `--actor` defaults to `operator:<OS user>` and is audit attribution, not authentication. See [goals and board](goals-board.md).

## Worker model setting

Personal settings live in `~/.config/orchd/config.toml` (`ORCHD_CONFIG_DIR` moves the directory), next to `init.toml`:

```toml
[worker]
model = "sol"   # sol | sonnet; omit for the factory default, sonnet
```

Precedence for the dispatch default: explicit `model` on the dispatch (flag) > env `ORCHD_WORKER_MODEL` > config file > `sonnet`.
Every layer is validated, even when a higher layer would win: an invalid flag, env value or file setting is an error, and nothing
falls through. Only an omitted flag (`null`) and an empty `ORCHD_WORKER_MODEL` count as unset; an explicit empty string, `0` or
`false` is an error. A missing file is fine; a directory at that path, invalid TOML, or a wrong shape (for example `worker = "sol"`
instead of a `[worker]` table) is an error that names the file. The value is read on every dispatch, so a running MCP server needs no restart.
`orchd config set/unset` edit only the `model` line (all other bytes, CRLF line endings included, stay as they were; a new line follows the file's own ending style) and refuse, writing nothing, unless the file re-parses to the same data apart
from `worker.model` (dotted or inline `worker` forms are refused: edit those by hand). The file keeps its permissions; a new one
is created `0600` in a `0700` directory.
`ORCHD_CONFIG_FILE` names an exact file and outranks `ORCHD_CONFIG_DIR`; it is a test/advanced hook, and `orchd config show` prints
which one chose the file (`file-source`).
`config show` also reports `worker-backend` with source `model-default`: `app-server` for effective `sol`, `exec` for `sonnet` (its existing Claude runtime). This derived field is read-only, not a config key; override it per dispatch with `backend=exec`. Retry retains a stored Codex attempt backend unless explicitly overridden. Existing exec workers are not migrated and can open a viewer only between turns. New app-server workers and Claude workers support native attach while busy or idle; app-server FIFO answers/followups wait until all viewer windows close.

Review always uses the other vendor than the author and has no setting.

```bash
orchd config show                   # every setting, its value and source (env | file | default), plus the file path
orchd config get worker-model       # prints the effective value, e.g. sonnet
orchd config set worker-model sol   # validates, then writes the file
orchd config unset worker-model     # remove the override from the config file; ORCHD_WORKER_MODEL, if set, still wins, else sonnet
orchd config get projects-dir       # effective project root (ORCHD_PROJECTS > file > checkout parent > ~/projects)
orchd config set projects-dir ~/projects   # absolute or ~/ path; same writer the first-run `orchd doctor` prompt uses
```

`worker-model` and `projects-dir` are written by the same in-place editor: only that one line changes, CRLF and the
file's mode are kept (a new file is 0600 in a 0700 directory), and it refuses to write when the result would not re-parse
to the old data plus the one change.

## Worker reporting

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd ack TASK_ID` | Acknowledge the assigned task before working. | `orchd ack TASK_ID` |
| `orchd progress TASK_ID "TEXT"` | Send an interim update; keep the task running. | `orchd progress TASK_ID "Tests passed; preparing review"` |
| `orchd ask TASK_ID "QUESTION"` | Submit a question or full action preview, then end the turn and await an answer. | `orchd ask TASK_ID "Approve this complete action preview: ..."` |
| `orchd report TASK_ID --status done\|blocked --summary TEXT [--evidence TEXT]` | Submit the final result and evidence; done still awaits verification. | `orchd report TASK_ID --status done --summary "Docs updated" --evidence "Commit and test results"` |
| `orchd verify TASK_ID [--timeout SECONDS]` | Verifier worker: rerun the locked verification command at the locked SHA. | `orchd verify TASK_ID` |

Quote text containing spaces. Report evidence defaults to an empty string; supply commits, tests and unverified items. Verify is for configured verification tasks; timeout is in seconds. Preview outward actions with ask and await an explicit answer. Report done does not imply deployment or acceptance.

## Internal and advanced

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd mcp [--role orch\|entry] [--entry NAME]` | Run the stdio MCP server for Orch or Entry tools. | `orchd mcp --role entry --entry desktop` |
| `orchd flush TASK_ID [--after-pid PID]` | Turn-shell integration: deliver queued answers after Codex exits. | `orchd flush TASK_ID --after-pid PID` |
| `orchd stats [--since TIME] [--json]` | Read source-backed task/token counts and cost estimates by Orch/task type. | `orchd stats --json` |
| `orchd close TASK_ID` | Stop a task worker and clean its worktree only when safe. | `orchd close TASK_ID` |

`mcp` defaults to role orch and entry desktop; entry names apply to entry role. Flush is shell integration, not a normal manual reporting step. App-server answers may return `queued` (supervisor busy) and are then sent automatically; if the server explicitly rejects `turn/start` for a non-rate-limit reason, automatic delivery pauses (text stays pending, reason in `supervisor_error` and the task note) until a new answer, `answer flush=true` or retry. A rate-limit rejection pauses automatic delivery until the reset time the error gives, else with a backoff capped near 5 minutes: no `turn/start`, journal rows or repeat notices while paused, a new answer does not lift it, `supervisor_error` and the note give the reason and next attempt time, and `answer flush=true` tries once immediately. `interrupt` is never queued while the supervisor is busy: it waits up to 10 seconds for the delivery lane and runs, else it fails with `interrupt not delivered: supervisor busy; retry` and leaves no pending text. A supervisor restart retries once. `stats --since` accepts local HH:MM today or ISO-8601 with Z/offset; default is all time and cost is estimated. Close stops a worker and may delete its worktree; follow task authorization.

## Common workflows

### Restart the Desktop-bound Orch

```sh
orchd orch restart --dry-run
orchd orch restart
```

Restart already stops the old Orch, confirms death using fresh runtime/socket probes, starts the replacement, adopts open tasks and rebinds Desktop when it was bound to the old Orch. You do not need a separate `binding` command. It leaves the daemon and task workers running. The model defaults to the old Orch's model; use `orchd orch restart OLD_ID --model sonnet` to select explicitly. Without OLD_ID, a missing/stale Desktop binding prints candidates and stops for selection.

After success, use the printed new Orch ID and first prompt `盤點上次` (take stock of previous work). If any step fails or is uncertain, follow the printed recovery commands and inspect task ownership before retrying: adopt can be committed even if notifications fail. An explicitly selected Orch that was not Desktop-bound leaves Desktop binding alone; use `orchd orch attach NEW_ID` to talk to it.

### Set up a new computer

1. Install the [requirements](../README.md#requirements), sign in to Claude Code/Codex, and configure your own Git/SSH access locally.
2. Install the public program and initialize the local templates:

   ```sh
   uv tool install git+https://github.com/NatChung/orchd.git
   uv tool update-shell
   orchd init
   orchd doctor
   ```

3. If you have a locally copied old Orch home, use `orchd init --from OLD_HOME` to copy its notes. Unmodified init templates are replaced; other existing files are kept. This copies home content; it does not transfer the database, running sessions or workers. Set personal paths/account mappings locally; see [configuration](../README.md#configuration). Do not commit credentials or machine settings.
4. Accept Claude trust manually in `~/orch/home`. Run `orchd binding`, open `~/orch/interface` in Codex Desktop, select the interface permissions created by init, request status first, then describe your work. `binding --status` checks the entry.

### Upgrade

```sh
orchd upgrade
orchd doctor
```

Upgrade installs the newest default-branch commit from uv's recorded source, dropping branch pins while keeping database/personal settings. Check the reported commit; the package version can remain unchanged. Existing sessions, daemon and MCP processes are not restarted and may keep old code. Finish active work and start new sessions/MCP processes with the new program; use the restart workflow when replacing an Orch.

For an archive/legacy install, reinstall from the public HTTPS source first; see [migration](../README.md#migrating-from-the-private-repository). Checkout installs must be updated in their own checkout.

See [dashboard answers and snapshot v2](dashboard-answers.md) for `entry-question answer`,
`entry-question un-snooze`, lifecycle fields and exact reply-to-worker receipts.

`snapshot --json` defaults to the legacy v1 contract. Select the new lifecycle and
reply receipt contract explicitly with `--schema-version 2`. `entry-question answer`
always prints JSON; it has no `--json` flag.

### Worker ask summary

Every worker `orchd ask ID "QUESTION"` must start with the following block.
Use plain language, about 5 lines and at most 300 characters; keep SHAs, paths,
logs, all required evidence and exact send previews after the standalone divider.
If there are no options, write `選項：無`. The impact line is optional.

```text
【給 Operator 的摘要】
要你決定：是否送出這份變更供審查？
選項：A 送出／B 暫緩
建議：選 A，測試已通過，可以交給 reviewer 檢查。
不回覆的影響：變更會保留在分支，等你決定。
---詳細---
完整送出預覽、測試輸出與證據放在這裡。
```

CLI `ask` warns on stderr without rejecting legacy bodies. MCP `ask_nat`
uses the same contract and returns an additive `warnings` list when the opening
block is missing. When quoting a worker question, put the Operator summary in
`text` and the full verbatim preview after `---詳細---`; quoting preserves the body.
For quote-only calls, the quoted worker body is checked instead of the quote prefix.
MCP warnings appear only in the result, after validation succeeds; they are not
also printed to stderr. Warnings do not mean the question was rejected or approved.

Home rules are updated automatically after program upgrade, using versioned steps, backups and managed blocks. See [migration behavior](upgrade-migrations.md) for `orchd upgrade --dry-run`, `--migrations-only`, custom paths and legacy first-run instructions.
