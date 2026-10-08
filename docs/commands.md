# orchd command reference

[繁體中文](commands.zh-TW.md)

Based on argparse in `orchd/cli.py` and dynamic fields in `orchd/goals.py`. All commands, subcommands, positional arguments and flags are covered below. Every level supports `-h` / `--help`.

## Remember these for daily use

| Command | Purpose |
| --- | --- |
| `orchd orch-restart` | Replace the Desktop-bound Orch, adopt open tasks and rebind. |
| `orchd list` | Check open tasks and health. |
| `orchd doctor` | Check machine readiness. |
| `orchd upgrade` | Update the uv-installed program. |

## Installation and maintenance

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd init [--from OLD_HOME] [--no-trust]` | Create home/interface templates and configure trust without overwriting edited templates. | `orchd init` |
| `orchd doctor [--profile example] [--json]` | Check machine readiness without changing configuration. | `orchd doctor` |
| `orchd upgrade` | Update the uv-installed program from its recorded source. | `orchd upgrade` |

`init --from` copies content from an old Orch home. It replaces destination files that still match an unmodified init template; other existing files are kept and listed. It skips `.git`, `.gitignore`, `.DS_Store`, `.claude`, `.codex`, and `AGENTS.md`; `--no-trust` skips Codex trust writes and Claude trust checks. Doctor exit codes: 0 passed, 1 required check failed, 2 required check unknown. `--profile example` is an optional fictional layout.

## Orch management

| Command and complete arguments | Description | Common example |
| --- | --- | --- |
| `orchd orch [--model sonnet\|opus] [--no-attach]` | Start a Claude Orch and attach by default. | `orchd orch --model sonnet` |
| `orchd orch-stop ORCH_ID` | Stop the named Claude Orch. | `orchd orch-stop ORCH_ID` |
| `orchd orch-restart [OLD_ID] [--model sonnet\|opus] [--dry-run]` | Replace an Orch, adopt its open tasks and rebind its Desktop entry. | `orchd orch-restart` |
| `orchd orchs [--all] [--json] [--restore ORCH_ID]` | List live/resumable Orchs; restore clears archival and resets death observation. | `orchd orchs` |
| `orchd attach ORCH_ID [--viewer]` | Attach to an existing Orch; resume its conversation if idle. | `orchd attach ORCH_ID` |
| `orchd adopt NEW_ORCH (TASK_ID ... [--from OLD_ORCH] \| --from OLD_ORCH) [--force]` | Operator: transfer open tasks to another Orch. | `orchd adopt NEW_ORCH --from OLD_ORCH` |

`orch` defaults to opus; `--no-attach` only starts it. `orchs` hides dead, unknown and archived Orchs by default; `--all` includes them and `--json` emits the complete inventory. `attach --viewer` opens Ghostty. After `NEW_ORCH`, provide at least one `TASK_ID` or `--from OLD_ORCH`; both may be used together. `adopt --from` transfers all open tasks or limits named task IDs; `--force` also permits transfer from an alive/unknown owner and notifies it.

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
| `orchd goal add\|set\|show\|list\|export` | Manage central goals through the five subcommands below. | `orchd goal list` |
| `orchd goal add REPO [GOAL_OPTIONS]` | Create a goal for a repository and record its audit actor. | `orchd goal add example --type goal --intent "Ship a reviewed change"` |
| `orchd goal set GOAL_ID [GOAL_OPTIONS]` | Update a goal and its audit history. | `orchd goal set GOAL_ID --status waiting --ball Operator` |
| `orchd goal show GOAL_ID` | Read a goal, linked tasks and complete history. | `orchd goal show GOAL_ID` |
| `orchd goal list [--repo REPO] [--status active\|waiting\|paused\|done]` | Read goals filtered by repository or status. | `orchd goal list --repo example` |
| `orchd goal export --md [--repo REPO]` | Print a Markdown snapshot of central goals. | `orchd goal export --md` |

Both `--since` flags use local HH:MM today. `watch` starts after the last message ID whose timestamp is before the cutoff, then follows new messages; it defaults to the last 30 minutes. `summary` selects tasks created at or after the cutoff and summarizes all messages belonging to those tasks; it defaults to all tasks. Board writes a file; rerun to refresh it.

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

`mcp` defaults to role orch and entry desktop; entry names apply to entry role. Flush is shell integration, not a normal manual reporting step. `stats --since` accepts local HH:MM today or ISO-8601 with Z/offset; default is all time and cost is estimated. Close stops a worker and may delete its worktree; follow task authorization.

## Common workflows

### Restart the Desktop-bound Orch

```sh
orchd orch-restart --dry-run
orchd orch-restart
```

Restart already stops the old Orch, confirms death using fresh runtime/socket probes, starts the replacement, adopts open tasks and rebinds Desktop when it was bound to the old Orch. You do not need a separate `binding` command. It leaves the daemon and task workers running. The model defaults to the old Orch's model; use `orchd orch-restart OLD_ID --model sonnet` to select explicitly. Without OLD_ID, a missing/stale Desktop binding prints candidates and stops for selection.

After success, use the printed new Orch ID and first prompt `盤點上次` (take stock of previous work). If any step fails or is uncertain, follow the printed recovery commands and inspect task ownership before retrying: adopt can be committed even if notifications fail. An explicitly selected Orch that was not Desktop-bound leaves Desktop binding alone; use `orchd attach NEW_ID` to talk to it.

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
