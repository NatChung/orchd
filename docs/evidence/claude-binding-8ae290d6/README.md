# Claude Orch binding: safety, tool parity and communication acceptance

The original launch uses `--restricted`; from Claude 2.1.290 it skips the explicitly requested messaging socket. The diagnosis `/tmp/orchd-890b5340/report.md` demonstrated the 2.1.289 -> 2.1.290 regression. This change uses explicit native tool/permission/settings configuration instead, without a downgrade or changing the live installation.

## What restricted enforced, and the replacement

2.1.291 CLI help defines restricted mode as removing command/code execution tools and WebFetch, ignoring user/project/local settings (managed and explicit settings remain), confining file tools to working directories, rejecting bypassPermissions, and reserving protected configuration/git/tool file writes for a person or permission handler. Embedded source also disables automatic memory and Chrome and prevents cloud-session escape. It is a CLI capability boundary; it does not prohibit Claude's own transcript/cache state writes.

| Boundary | Replacement |
| --- | --- |
| No Bash, PowerShell, REPL, WebFetch or new code-running tools | Explicit native tool list from the original 2.1.289 restricted inventory, including background-only Artifact/AskUserQuestion and Agent/Task aliases; unknown future tool names are not automatically added |
| Ignore user/project/local settings | `--setting-sources ""`; managed policy and explicit settings retained |
| Only orchd MCP | Same `--strict-mcp-config`, same orchd MCP server; no new filesystem MCP, hook or network tool |
| Read/Grep/Glob/LSP confined to home + cached input images | Supported native `permissions.blockReadsOutsideWorkingDirectories=true`, verified in the installed permission schema and by actual denials |
| Edit/Write limited to groups/ | Original absolute canonical `Edit(//<home>/groups/**)` rule retained (Edit path rules cover Write/NotebookEdit); dontAsk denies everything else |
| Protected config/git/tool file writes cannot auto-approve | Explicit deny rules for the Orch home's .claude/, .git/, .mcp.json, CLAUDE.md and AGENTS.md; all outside-group writes remain unallowed |
| No bypass mode | `permissions.disableBypassPermissionsMode=disable` + dontAsk |
| No implicit Chrome or auto-memory | `--no-chrome`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` |
| Preserve skills/artifact work | Skill and Artifact retained; no `--disable-slash-commands` and no `--bare` |

All model-accessible tools continue through Claude's native permission checks. A wide Read allow is replaced with canonical home/image scopes. The normal groups write allowance is unchanged. At Nat's explicit follow-up request, deny patterns now exclude hidden files/directories and AGENTS.md, CLAUDE.md, settings.json, settings.local.json and mcp.json anywhere within groups/. These exclusions are new requested restrictions: baseline 2.1.289 actually allowed edits to those nested fixtures; they were not its active protected configuration. This difference is disclosed rather than described as old-mode parity. In macOS sandbox tests, `/tmp` and `/private/tmp` must be spelled consistently with the canonical allow rule; the original configuration has the same alias behavior. Fixtures and tool requests use canonical paths, matching the production `/Users/...` path's unaliased spelling.

## Exact tool set comparison

AST comparison confirmed the baseline start_orch method still matches the installed package. Baseline: the exact `Runtime.start_orch` method from commit `d97b8da500353e264f966458a3dc10365ecf7d85`, launched on 2.1.289 with its original restricted flag/settings. Candidate: this worktree launched on 2.1.291. The CLI startup tool inventories are identical (including all 16 orchd MCP tools); `native-parity.json` attaches both lists. The background sessions also report Artifact and the native Agent/Task aliases, while the artifact-capabilities skill actually loads in both. Print-mode inventories alone do not enumerate all background-only tools, so that evidence is kept separate.

| Tool/function | Original 2.1.289 restricted | Candidate 2.1.291 | Result |
| --- | --- | --- | --- |
| Read home/group file | read OLD_NOTE | read OLD_NOTE | PASS |
| Grep home/groups | found fixture | found fixture | PASS |
| Glob home/groups | found fixture | found fixture | PASS |
| Cached input image Read | native Read returns image | native Read returns image | PASS; outputs in images.json |
| Edit groups note | changed OLD_NOTE -> NEW_NOTE | changed OLD_NOTE -> NEW_NOTE | PASS |
| Write groups note | created NEW_FILE | created NEW_FILE | PASS |
| Create then Edit groups .md/.txt/.html/.json | all four NEW_NOTE | all four NEW_NOTE | PASS; groups.json |
| Nested groups hidden/config/instruction Edit | seven fixtures edited | all seven explicitly denied, unchanged | Requested new restriction; groups.json |
| New repo/home-outside-groups Write | not separately attempted | dontAsk denial, file absent | PASS candidate; groups.json |
| New groups hidden/instruction Write | not separately attempted | explicit denial, file absent | PASS candidate; groups.json |
| ToolSearch/deferred orchd loading | tool_reference + inbox [] | tool_reference + inbox [] | PASS |
| Skill | artifact-capabilities invoked, contract 0.2.69 | same invocation/contract | PASS |
| Artifact | present in background session | present in background session | PASS for availability; publishing not executed |
| Outside Read/Grep/Glob | three native restricted denials | three native read-block denials | PASS |
| Edit home file outside groups | native dontAsk denial; unchanged | native dontAsk denial; unchanged | PASS |
| Bash | absent from tool inventory | absent from tool inventory | PASS |
| Other originally exposed native tools | Task, CronDelete, CronList, DesignSync, EnterWorktree, ExitWorktree, ListAgents, NotebookEdit, ReportFindings, ScheduleWakeup, SendMessage, TaskStop, WebSearch | same startup list | PASS for availability; not all individually invoked |

A native Agent was also started on 2.1.291 and its child reported Bash unavailable (native-agent.json). The original restricted Agent started too, but its final child report was not captured; only invocation parity is claimed for that extra check.

Neither Artifact nor any external connector was used to publish/send data. The settings-file edit requested by a peer was declined by the model in the broad parity probe; that refusal is not counted as enforcement evidence. The explicit protected-path deny selection is unit tested, and a separate native protected-file fixture probe is recorded in protected.json. No inference from an LLM refusal is used to call a permissions test passing.

## Full communication path on 2.1.291

`communication.json` records real model tool_use/tool_result events and the isolated DB facts. No fake socket, model session or Runtime was used.

| Step | Result | Evidence |
| --- | --- | --- |
| Start/bind Orch | PASS | real socket, job and session; entry bind reports alive and notice sent |
| Deliver orchd message to Orch | PASS | real worker ask/report wakes the Orch, followed by actual inbox calls |
| Deferred MCP loading | PASS | ToolSearch returns dispatch/inbox/answer/send_to_nat references |
| dispatch | PASS | real Sonnet worker in a fresh worktree of a synthetic local git repo |
| inbox | PASS | reads ack/question, then reads worker done report |
| answer | PASS | delivered=1, pending=0; worker subsequently reports answer received |
| send_to_nat | PASS for MCP call/queue | exact SYNTHETIC_E2E_DONE stored in isolated entry DB; delivery pending because no Desktop thread is connected |

This proves the Orch/worker notification and question/answer loop. Desktop entry transport, real Nat receipt and actual Artifact publishing are not claimed as tested. The test intentionally has no link to the real Desktop thread. A fresh sandbox required a bypass disclaimer boolean for the full-permission worker; it was seeded only into the disposable HOME, without modifying any live permission setting.

## Isolation, tests and install steps

Each case uses a fresh 0700 HOME, ORCHD_HOME/root/projects, worktree executable and matching-version foreground Claude daemon with a separate control socket namespace. Authentication is the real logged-in access token read into memory from Keychain, using the supported OAuth-token env var; no refresh token or auth file was copied and no token was printed. Only onboarding, sandbox trust and (for worker dispatch) acceptance metadata were seeded. The fixture repo's origin is a local bare repo. No installation, global account switch, live config/DB/socket connection, launchd operation or live process restart was executed.

Before/after metadata in native-parity.json shows live PIDs 71930/4740 creation times/executable/socket arguments unchanged and unchanged live DB/control/Orch socket inode/mtime/size. Normal ack/progress and other work may update the live WAL: this does not claim no concurrent live activity. Cleanup removes exact sandbox-owned processes, HOME/state, daemon namespace, Orch/worker sockets and image trees. Earlier cleanup evidence is in cleanup.json; the two additional group/write probe roots each record survivors=[] and before/after live metadata in groups.json. Their HOME/state/control/Orch sockets and image trees were removed too. Redacted logs/harnesses remain under /tmp/orchd-8ae290d6*.

Tests: `python -m unittest discover -s tests -t .`: **705 tests, OK** (full-suite.txt); targeted native tool selection, config namespace, missing binary, socket failure cleanup, CLI errors and resume: **14 tests, OK** (targeted-tests.txt), Both commands were rerun after the final group-deny configuration edits; full suite 264.746s and targeted 0.110s (see exact output). Startup logs resolve the Claude binary and version and identify the cross-session JSONL protocol; an existing session without a socket prints a hint immediately. A monotonic deadline bounds startup polling, with at most one in-flight agents query extending it. Failed startup stops only its newly created job. Binding handles RuntimeError/OSError concisely. CLAUDE_CONFIG_DIR is kept consistently for launch/trust.

For Nat after a separate reviewer merges: `orchd upgrade`, then choose a suitable time to run `orchd binding --new` and `orchd binding --status`. A branch trial would be `uv tool install --force --refresh git+ssh://git@github-NatChung/NatChung/orchd@orchd/8ae290d6`. **None executed.** Running Orchs retain their original launch settings; no automatic replacement or DB migration is performed.

Primary references: native 2.1.291 `--help` and embedded settings schema; [settings semantics](https://code.claude.com/docs/en/settings), [native permissions](https://code.claude.com/docs/en/permissions).
