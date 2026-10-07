# orchd

[繁體中文](README.zh-TW.md)

orchd manages tasks across AI coding agents. An **Orch** breaks down work and dispatches it; each **worker** handles one task in an isolated Git worktree and reports through `ack`, `progress`, `ask`, and `report`. Tasks, questions, deliveries, and goals are stored in a local SQLite database. A worker's completion report still needs its evidence checked.

## Requirements

- macOS, Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).
- [Claude Code](https://claude.com/claude-code), signed in and able to run background sessions. The current transport uses `--bg` and an experimental messaging socket; CLI updates can affect compatibility.
- [Codex CLI](https://github.com/openai/codex), signed in when using Codex workers. Codex Desktop is an optional conversation entry point.
- tmux for background sessions. Ghostty is optional for attach/viewer windows. GitHub CLI is optional for issue tracking and account routing.
- Python dependencies include psutil and websocket-client. App-server workers are opt-in; the default worker backend is exec.

Sign in through each provider's CLI and accept the trust prompts for your projects and Orch home. Credentials stay in the providers' local login settings.

## Install and update

```sh
uv tool install git+https://github.com/NatChung/orchd.git
uv tool update-shell
orchd doctor
```

`doctor` checks the machine without changing its configuration. Before initialization, it reminds you to set up the Orch home. For development from a checkout, use `bin/orchd`.

```sh
orchd upgrade
```

Upgrade reads the source recorded by uv and installs the latest commit on that source's default branch. It drops branch pins and leaves the database and personal settings in place. Existing sessions and MCP processes can keep the old code; start new sessions when convenient. Upgrade does not restart the daemon. Update checkout installations in their own checkout.

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
| Projects directory | `~/projects` | `ORCHD_PROJECTS` |

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
orchd binding --status
orchd list
orchd watch
```

Init creates the home and interface templates and backs up Codex configuration before updating trust. It preserves existing templates you have changed. Accept Claude trust manually in the home. Then open `~/orch/interface` in Codex Desktop, select the interface permissions created by init, request status first, and describe your work. You can also start a Claude Orch with `orchd orch` or open a Codex Orch in `~/orch/home`.

The Orch dispatches through tools provided by `orchd mcp`. Workers execute in their assigned worktrees and report with:

```sh
orchd ack TASK_ID
orchd progress TASK_ID "Current progress"
orchd ask TASK_ID "Question or complete action preview"
orchd report TASK_ID --status done --summary "What changed" --evidence "Commit, tests, and unverified items"
```

After asking, wait for the answer. Preview outward messages, repository creation, merges, and other approval-gated actions before executing them. A worker report marked done is a delivery awaiting verification; deployment and acceptance are separate decisions.

```sh
orchd orchs
orchd attach ORCH_ID
orchd summary
orchd goal list
orchd board --html /tmp/orchd-board.html
orchd --help
```

The board is a local read-only HTML snapshot; run the command again to refresh it. See [CONTEXT.md](CONTEXT.md), [architecture decisions](docs/adr/), [the entry](docs/entry.md), [permissions](docs/orch-permissions.md), [goals and the board](docs/goals-board.md), and [review/merge policy](docs/review-merge-policy.md).

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
