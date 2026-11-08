# orchd

[繁體中文](README.zh-TW.md)

orchd manages tasks across AI coding agents. An **Orch** breaks down work and dispatches it; each **worker** handles one task in an isolated Git worktree and reports through `ack`, `progress`, `ask`, and `report`. Tasks, questions, deliveries, and goals are stored in a local SQLite database. A worker's completion report still needs its evidence checked.

## Requirements

- macOS or native Windows, Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).
- [Claude Code](https://claude.com/claude-code), signed in and able to run background sessions. The current transport uses `--bg` and an experimental messaging socket; CLI updates can affect compatibility.
- [Codex CLI](https://github.com/openai/codex), signed in when using Codex workers. Codex Desktop is an optional conversation entry point.
- tmux for POSIX exec background sessions. Native Windows uses the Codex app-server worker backend. Ghostty is optional for macOS attach/viewer windows. GitHub CLI is optional for issue tracking and account routing.
- Python dependencies include psutil and websocket-client. New sol workers default to app-server for native TUI attach while busy or idle. Explicit `backend=exec` remains available; sonnet and existing workers are unchanged.

Sign in through each provider's CLI and accept the trust prompts for your projects and Orch home. Credentials stay in the providers' local login settings.

## Install and update

```sh
uv tool install git+https://github.com/NatChung/orchd.git
orchd install
uv tool update-shell
orchd doctor
```

`uv tool install` installs the package only; run `orchd install` to provision homes and disabled channel templates. The installation wrappers run provisioning after a successful package install. `orchd install --dry-run --json` previews without writing; `orchd install --no-trust` preserves existing trust settings. Channel credentials use OS secret references; installation does not authorize accounts or enable sources.

`doctor` checks the machine without changing its configuration. Before initialization, it reminds you to set up the Orch home. For development from a checkout, use `bin/orchd`.

On the first terminal run, if no projects directory is configured or detected, `orchd doctor` asks for the parent directory of your Git repositories and saves it to `config.toml`; it does not ask again. `--json`, `--no-input`, and non-interactive runs only check and never prompt or save.

```sh
orchd upgrade
```

Upgrade reads the source recorded by uv. On POSIX it follows that source's default branch and drops branch pins. On Windows it retains the recorded source and revision. It preserves task data and personal settings, then runs versioned home-rule migrations with backups. See [home migration behavior](docs/upgrade-migrations.md) for managed blocks, previews, retries, and the first upgrade from a legacy version. Existing sessions and MCP processes can keep the old code; start new sessions when convenient. Upgrade does not restart the daemon. Update checkout installations in their own checkout.

## Native Windows and PowerShell

Install uv and Git, then install and sign in to the provider CLIs listed above. Use a PowerShell terminal for these commands:

```powershell
# From a checkout containing the Windows upgrade fix:
.\scripts\install-windows.ps1 -Source (Get-Location).Path
uv tool update-shell
# Open a new PowerShell terminal after updating PATH.
orchd --help
orchd init
orchd config set projects-dir "$HOME\projects"
orchd binding
```

The script also migrates an older uv installation: it installs into a **new** directory and exposes the launcher at the usual uv bin path. Keep the old directories; a running Desktop chat can still import modules from them. Do not run an in-place `uv tool install --force` or `uv tool upgrade orchd` against an installation used by a live chat.

For subsequent updates, run `orchd upgrade`. If you installed from a checkout, update that checkout first; for a Git install, pass a reviewed `git+https://…/orchd.git@branch` as `-Source` to track the branch. An exact Git commit stays pinned. New commands use the updated installation; an already running MCP process can finish on its original version. No Desktop restart or new chat is required.

Each project is a Git repository under the configured projects directory. After binding an Orch, dispatch a Codex worker and read its reports directly from PowerShell:

```powershell
orchd dispatch --repo example-repo --model terra --backend app-server `
  --title "Read the README" --instructions "Read README.md and report its first heading." `
  --done-when "The first heading is included in the report." `
  --model-reason "Read-only repository check" --task-type investigation
orchd list
orchd inbox
```

Dispatch returns a `task_id`. `inbox` reads and acknowledges the owning Orch's unread worker messages, so save any report you need. A standalone terminal uses the Desktop binding, or accepts `--orch-id` for an explicitly selected registered Orch. It reports an error if no owner can be selected.

For a Codex Desktop conversation, open the actual `$HOME\orch\interface` folder as a project and start the chat from that project. Its `.codex\config.toml` specifies `gpt-6.1-terra`, the `interface` permission profile, read-only access, disabled shell tools, and no network. Permission profiles are resolved in project context, so the host-wide picker may omit `interface`. Verify that the project configuration is loaded and request the bound Orch's status. Changing the global default permission profile is unnecessary. Reopen the chat after configuration changes. See [the entry setup](docs/entry.md) for managed instruction updates.

Windows attachment copying currently reports `unsupported_platform`. Ghostty/macOS viewer integration and the POSIX exec-worker transport remain platform-specific. Claude background sessions still depend on the installed CLI's experimental messaging transport. Some Codex Windows sandbox versions reject restricted child-process reads with an effective `:root` read-access error. The MCP-only interface keeps its restricted policy and supplies its instructions through project TOML so it can start without that document-read operation.

## Migrating from the private repository

The former private repository is [orchd-archive](https://github.com/NatChung/orchd-archive). The public repository starts with clean history. Keep existing private-history clones separate:

```sh
# In your old clone, preserve its connection to the archive.
git remote set-url origin https://github.com/NatChung/orchd-archive.git
# Outside that clone, choose a new, unused directory.
git clone https://github.com/NatChung/orchd.git orchd-public
uv tool install --force --refresh git+https://github.com/NatChung/orchd.git
uv tool list
orchd --help
orchd upgrade
```

Reinstalling changes uv's update source; changing a clone's remote alone does not. Do not pull the public history into a private-history clone or depend on GitHub's old-name redirects. Public upgrades use HTTPS and require no GitHub token. `orchd upgrade` warns about archive and legacy SSH installation sources and prints the reinstall command. Older installed versions need the explicit reinstall above before they gain this warning.

Compare the commit reported by `orchd upgrade` with public `main`; the package version alone can remain unchanged. Your database and personal settings stay in place. Finish active tasks before starting new sessions or MCP processes with the new program; upgrade does not restart them.

## Issues and contributions

Use [public issues](https://github.com/NatChung/orchd/issues) for general bugs and improvements, with synthetic examples and redacted logs. Keep customer details, personal information, internal URLs, and sensitive configuration in an access-controlled private tracker (for existing private users, orchd-archive). Never post secrets in any issue. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Configuration

| Purpose | Default | Override |
| --- | --- | --- |
| Database and task state | `~/.local/share/orchd` | `ORCHD_HOME` |
| Personal settings | `~/.config/orchd` | `ORCHD_CONFIG_DIR` |
| Orch root | `~/orch` | `ORCHD_ROOT` |
| Orch home | `~/orch/home` | `ORCHD_ORCH_HOME` |
| Desktop entry | `~/orch/interface` | `ORCHD_INTERFACE_HOME` |
| Computer-use session home | `~/orch/gpt-computer-use-worker` | under `ORCHD_ROOT` |
| Projects directory | parent of a checkout under HOME, else `~/projects` | `projects_dir` in `config.toml`, `ORCHD_PROJECTS` |

`orchd init` also creates the computer-use session home with managed `AGENTS.md` rules and a `reports/` evidence folder. Re-running init refreshes that managed block with backups and preserves user content outside it. The package manager does not provision homes automatically: run `orchd install` after a bare package install. The official installer wrappers run it after installation. Desktop dispatch is design-only; see [the proposed backend](docs/design/computer-use-worker.md).

`config.toml` in the personal settings directory sets the projects directory shared by doctor and dispatch (`~/` or an absolute path); `ORCHD_PROJECTS` overrides it. The value does not depend on the current working directory, so Desktop and background dispatch use the same location:

```toml
projects_dir = "~/GitProjects"
```

`init.toml` controls extra paths the Orch home can read and apps to disable. Set these for your own work scope; the example includes no real connector IDs:

```toml
[home]
read = ["~/AGENTS.md", "~/agent-guidance"]
disabled_apps = []
```

An optional `gh-accounts.json` routes workers' GitHub CLI calls by repository owner without changing the global active account:

```json
{"ExampleOwner": "example-personal", "*": "example-work"}
```

Use `ORCHD_GH_ACCOUNTS` to select another file. Without a mapping, gh keeps its original behavior; an existing `GH_TOKEN` is preserved. Keep actual accounts, SSH keys, connectors, and credentials in your local configuration.

`orchd doctor --profile example` and `python3 scripts/setup-wizard.py --profile example` are optional fictional examples. They do not mean your machine needs four GitHub accounts. Most users can run `orchd doctor`. To customize the example profile:

- Doctor: `ORCHD_PROFILE_GH_ACCOUNTS` and `ORCHD_PROFILE_SSH_ALIASES` (comma-separated), plus `ORCHD_PROFILE_CREDENTIAL_DIR`.
- Setup wizard: `ORCHD_PROFILE_ACCOUNTS_JSON`, such as `{"example-personal": ["github-personal", "id_ed25519_personal"]}`. A host alias can be null.
- Both: `ORCHD_PROFILE_CONNECTOR_ROOT`.

Doctor checks credential files for existence only. The wizard prints manual steps; it does not install software, sign in, or write configuration.

## Basic use

```sh
orchd init
orchd binding
orchd list
```

Accept Claude trust manually in `~/orch/home`, then open `~/orch/interface` in Codex Desktop, select the interface permissions created by init, request status first, and describe your work.

`binding` requires `init` first. It starts an Orch when no binding exists; an offline bound Orch causes an error and requires `orchd binding --new` to start a replacement and rebind. A session retired by Claude for idling can resume in place.

`orchd orch restart --dry-run` only reads and prints the plan; it does not execute it.

For daily use, remember `orch restart` (replace an Orch, including adopt and rebinding its existing Desktop entry), `list` (tasks), `doctor` (checks), and `upgrade` (updates). See the [grouped command reference](docs/commands.md) for every command and flag, worker reporting, and restart/new-computer/upgrade workflows. A worker report marked done still awaits verification; deployment and acceptance are separate decisions.

Entry messages can carry one optional issue ref; `orchd entry thread --issue-refs github:example/project#40 --limit 3 --json` reads recent local question/answer summaries from a private DB copy. See [entry issue threads](docs/entry.md#issue-linked-local-threads) for MCP/CLI writes, tag clearing, privacy and the JSON contract.

See also [CONTEXT.md](CONTEXT.md), [architecture decisions](docs/adr/), [entry](docs/entry.md), [permissions](docs/orch-permissions.md), [goals and board](docs/goals-board.md), and [review/merge policy](docs/review-merge-policy.md).

See [quota observations](docs/quota.md) for `orchd quota`, MCP `quota_status`, official receipts and dispatch shadow advice. Shadow advice does not change the selected model or backend.

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

## Development and checks

```sh
uv venv
uv pip install -e .
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q orchd scripts tests
uv build
```

Automated tests mainly use mocks and temporary environments. For manual app-server acceptance, run `python3 scripts/app_server_e2e.py` with tmux, Codex authentication, and an isolated temporary environment. This does not imply every GUI, provider, or version combination has been verified.

Use a branch and pull request for changes, with a different reviewer checking the result. Redact credentials and private information before posting issues or logs.

## License

[MIT](LICENSE).

### Read-only PR review inventory

Run `orchd pr-watch OWNER/REPO [OWNER/REPO ...]` to print JSON for open PRs on
`orchd/` branches or with the `ai-agent` label. The command only reads GitHub.
It uses the PR author as the reply owner. Inline replies are matched by thread;
issue comments have no thread, so any later author comment is treated as a reply.
The cutoff uses the latest available head commit date, force-push time, or restore
time from the PR timeline. This is a lower bound and may precede the last push.
When no relevant timeline data exists, the cutoff is marked uncertain and older
comments remain candidates. Commit time is not necessarily a push time.
Review the linked conversations before acting on the suggested action.
