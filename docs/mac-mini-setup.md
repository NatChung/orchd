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
runs the commands it prints. Paths come from the current `HOME` (override with `--home` / `--projects`), with one
exception: the `orchd checkout present` step always checks the checkout the wizard file itself lives in
(`scripts/../bin/orchd`), whatever `--home` / `--projects` say. Its suggested `git clone` target is under `--projects`.
Statuses: `pass`, `missing`, `unknown` (could not verify; never counted as pass), `manual`.

Commands it runs are a fixed read-only allowlist (exact argv, enforced in code and pinned by a test): `--version`
and `claude --help`, `claude auth status`, `codex login status`, `gh auth status`, `git config --global --get
user.name|user.email`, and `ssh-keygen -l -f -` (public-key text on stdin, never a path). Anything else is refused. `--strict` also applies to
`--interactive` (quitting early with open steps exits 1).

Each open step shows who runs it (Nat, at the target machine's own terminal), the exact command, the source it was
checked from, and the receipt that proves it worked.

Worker choices: `sol` (GPT-6.1 Sol on Codex, default and preferred) and `sonnet` (claude-sonnet-5-5,
when switching vendors). Both CLIs are checked by the wizard; doctor reports Codex as optional for machines
using only Claude. Per-repo Claude trust checks apply to Sonnet workers; Sol runs on Codex.

## 2. Generic environment vs Nat profile

Generic: git, python3 3.11+, Claude Code (`--bg`), Codex, gh, the three logins, a global git author, an SSH key,
orchd checkout, Orch home, Orch trust in Claude and Codex, Orch `.codex/config.toml` paths (flags another
machine's `/Users/<x>`), `orchd` MCP in `~/.codex/config.toml`, per-repo Claude trust, writable state and `/tmp`.

The wizard itself starts on any Python 3. Without `tomllib` (3.9/3.10, e.g. macOS `/usr/bin/python3`) the three checks that
read Codex TOML (Orch Codex trust, Orch `.codex/config.toml` paths, `orchd` MCP) report `unknown` with that reason; they are
never guessed with regex. Everything else runs unchanged. Run it with Python 3.11+ for the full result.

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
- SSH keys: private key files are never opened. The `.pub` is opened without following symlinks and must be a
  regular, single-link file of at most 16 KiB holding exactly one public-key line; a symlink (even to a real public
  key), a hard link, a FIFO or a copy of private key material is refused before anything runs. Only then is its text
  fed to `ssh-keygen -l -f -`. Whether the private half matches it, and whether GitHub accepts it, are not checked.
- Login state falls back to `unknown` when `claude auth status` / `codex login status` fail or are unsupported. Codex
  passes only when its first line is `Logged in using ChatGPT` or `Logged in using an API key`; any other wording,
  even with exit 0 (e.g. `Unable to determine whether you are logged in`), is `unknown`.
- `--home` pointing anywhere but the HOME of the user running the wizard: gh, Claude and Codex keep logins in that
  user's keyring, so their login steps are `unknown` and are not run. `git config --global` is run with
  `HOME=<--home>` so it reads the target; other commands keep the caller's environment and never write into `--home`.
- Not zero-write for the caller: `gh --version` and `codex --version` (run in the default mode and with `--home`) may
  write their own startup state under the caller's `HOME` or temp dir (for example a device id file, or a temp arg0
  file). The wizard adds no install, login, trust or config write of its own, and nothing goes into `--home`.
- Environment overrides make the matching provider `unknown` (names reported, values never shown): git
  `GIT_CONFIG*`, `XDG_CONFIG_HOME`; gh `GH_CONFIG_DIR`, `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GH_HOST`;
  Claude `CLAUDE_CONFIG_DIR`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`; Codex
  `CODEX_HOME`, `OPENAI_API_KEY`. `CLAUDE_CONFIG_DIR` / `CODEX_HOME` also make the trust and MCP file checks `unknown`.
- orchd's own path variables `ORCHD_ORCH_HOME` (Orch home) and `ORCHD_HOME` (state dir): when `--home` is the HOME of
  the user running the wizard they are honoured, just as orchd would. With a foreign `--home` they describe the caller's
  machine, so the steps that depend on them are `unknown` (name reported, value never shown): `ORCHD_ORCH_HOME` →
  `orch-home`, `trust-orch-claude`, `trust-orch-codex`, `orch-config-paths`; `ORCHD_HOME` → `orchd-home`. The target's
  default paths are not checked in their place either, since orchd on the target reads its own environment.
- Orch `.codex/config.toml` is parsed as TOML; a home dir ends at the next `/` or the end of the string, so
  `/Users/Nat Space` is foreign to HOME `/Users/Nat`. Unparseable TOML is `unknown`. A shell-like string such as
  `cd /Users/x && y` is flagged whole (errs to `missing`, never to `pass`).
- `orchd doctor` (`orchd/doctor.py`, merged) is not run or parsed by the wizard; that step is always `unknown` and
  Nat runs doctor directly (receipt row 2). In an older checkout without `orchd/doctor.py` the step says so.

Logins use each CLI's own browser flow; nobody pastes a token. Generate new SSH keys on the mini and register the
public key. Do not copy private keys or export the old keychain.

## 4. Pilot receipts (Nat on the mini, or a worker Nat authorises there)

| # | Who / where | Command | Receipt |
|---|---|---|---|
| 1 | Nat, mini | `python3 scripts/setup-wizard.py --profile all --strict` | exit 0, or each remaining non-pass step has a written reason |
| 2 | Nat, mini | `bin/orchd doctor` | exit 0 (`required checks: all pass`); exit 1 = a required check failed, exit 2 = a required check unknown, neither is a pass; output pasted in the issue |
| 3 | Nat, mini | `ORCHD_HOME=$(mktemp -d)`, dispatch a task to a scratch repo | task id, worker running in `orchd list` |
| 4 | worker | `orchd ack`, `orchd progress`, `orchd report --status done` | all three visible in `orchd watch` |
| 5 | independent reviewer | read the pilot diff, write a review receipt | reviewer is not the pilot author |
| 6 | Nat | `orchd close <id>`; delete temp `ORCHD_HOME` and scratch repo | worktree gone, no leftovers |

Paste the outputs of 1–5 into issue #3, then Nat decides whether to close it.
