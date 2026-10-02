# Moving orchd to the Mac mini (issue #3, part B)

This is guidance and a receipt checklist. Nothing here has been run on the Mac mini. Issue #3 stays open until the
receipts in section 4 exist.

## 1. What the wizard does

```bash
python3 scripts/setup-wizard.py                    # generic environment, read-only plan
python3 scripts/setup-wizard.py --profile all      # plus the Nat profile (4 GitHub accounts, connectors, worker CLIs)
python3 scripts/setup-wizard.py --json             # same plan as JSON
python3 scripts/setup-wizard.py --interactive      # walks open steps; you act, Enter re-checks
python3 scripts/setup-wizard.py --strict           # exit 1 unless every step is `pass`
```

It checks and prints. It never installs, logs in, trusts, writes config, copies tokens, exports the old keychain, or
runs the commands it prints. All paths come from the current `HOME` (override with `--home` / `--projects`).
Statuses: `pass`, `missing`, `unknown` (could not verify; never counted as pass), `manual`.

Commands it runs are a fixed read-only allowlist (exact argv, enforced in code and pinned by a test): `--version`
and `claude --help`, `claude auth status`, `codex login status`, `gh auth status`, `git config --global --get
user.name|user.email`, and `ssh-keygen -l -f <key>.pub`. Anything else is refused. `--strict` also applies to
`--interactive` (quitting early with open steps exits 1).

Each open step shows who runs it (Nat, at the target machine's own terminal), the exact command, the source it was
checked from, and the receipt that proves it worked.

## 2. Generic environment vs Nat profile

Generic: git, python3 3.11+, Claude Code (`--bg`), Codex, gh, the three logins, a global git author, an SSH key,
orchd checkout, Orch home, Orch trust in Claude and Codex, Orch `.codex/config.toml` paths (flags another
machine's `/Users/<x>`), `orchd` MCP in `~/.codex/config.toml`, per-repo Claude trust, writable state and `/tmp`.

Nat profile: the four GitHub accounts (gh login + new SSH key + `~/.ssh/config` alias), per-repo git emails,
nat-email / nat-slack / nat-line connector directories, and codegraph / rtk / gcloud / fastlane (report only).

## 3. What the wizard cannot verify (say so, don't guess)

- `--messaging-socket-path` is a hidden Claude flag; only `--bg` in `--help` is checked.
- Whether GitHub accepts an SSH key: needs a network call, which the wizard does not make. Nat runs `ssh -T <alias>`.
- Connector token presence: location differs per connector and secrets are never read. Status is `unknown`.
- Per-repo git author email: depends on which identity owns the repo.
- Login state passes only on affirmative output (`gh auth status` exit 0 with logged-in accounts; `claude auth status`
  `loggedIn: true`; `codex login status` saying logged in). A non-zero `gh` exit (e.g. one invalid token) is `unknown`.
  Credential files and `apiKey`-style config values are never read as proof or shown; of `~/.claude.json` and
  `~/.codex/config.toml` only trust flags and `mcp_servers.orchd` presence are kept.
- SSH keys: private key files are never opened; a key counts only if its `.pub` passes `ssh-keygen -l`. Whether the
  private half matches it is not checked.
- Login state falls back to `unknown` when `claude auth status` / `codex login status` fail or are unsupported.
- `orchd doctor` (PR #22) is not run or parsed. If absent from the checkout the step says so; it is never a pass.

Logins use each CLI's own browser flow; nobody pastes a token. Generate new SSH keys on the mini and register the
public key. Do not copy private keys or export the old keychain.

## 4. Pilot receipts (Nat on the mini, or a worker Nat authorises there)

| # | Who / where | Command | Receipt |
|---|---|---|---|
| 1 | Nat, mini | `python3 scripts/setup-wizard.py --profile all --strict` | exit 0, or each remaining non-pass step has a written reason |
| 2 | Nat, mini | `bin/orchd doctor` (after PR #22 merges) | exit 0, no `missing`/`unknown`, output pasted in the issue |
| 3 | Nat, mini | `ORCHD_HOME=$(mktemp -d)`, dispatch a task to a scratch repo | task id, worker running in `orchd list` |
| 4 | worker | `orchd ack`, `orchd progress`, `orchd report --status done` | all three visible in `orchd watch` |
| 5 | independent reviewer | read the pilot diff, write a review receipt | reviewer is not the pilot author |
| 6 | Nat | `orchd close <id>`; delete temp `ORCHD_HOME` and scratch repo | worktree gone, no leftovers |

Paste the outputs of 1–5 into issue #3, then Nat decides whether to close it.
