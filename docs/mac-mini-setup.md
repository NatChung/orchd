# Setting up orchd on another macOS machine

This is a read-only setup guide. Two tools help: `scripts/setup-wizard.py` lists what is still open on the target
machine, and `orchd doctor` checks whether orchd can run there. Neither installs software, signs in, or writes
configuration. Run the receipt checklist in your own isolated environment.

You need only the generic requirements below. The example profile is optional and fictional: skip it unless your
setup uses several GitHub accounts, SSH host aliases or connectors. Private skills, connectors, multiple accounts and
cloud tools are not needed for the core flow (init, dispatch, worker reports, close).

## 1. What the wizard does

```bash
python3 scripts/setup-wizard.py                    # generic environment, read-only plan (the default)
python3 scripts/setup-wizard.py --profile all      # plus the optional example profile
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

Each open step shows who runs it (Operator, at the target machine's own terminal), the exact command, the source it was
checked from, and the receipt that proves it worked.

Worker choices: `sonnet` (claude-sonnet-5-5, factory default), `terra` (GPT-6.1 Terra), `sol` (GPT-6.1 Sol), `fable` and `astra`; the alias table is configurable (see commands.md).
Change the dispatch default with `orchd config set worker-model sol` (or `ORCHD_WORKER_MODEL`; see docs/commands.md).
Per-repo Claude trust checks apply to Claude workers; Codex models run on Codex.

## 2. Generic requirements vs the optional example profile

### Generic requirements

The wizard's generic plan covers: git, python3 3.11+, Claude Code (with `--bg` in its help), Codex, gh, the three
logins, a global git author, an SSH key, the orchd checkout, the Orch home, Orch trust in Claude and Codex, Orch
`.codex/config.toml` paths (flags another machine's `/Users/<x>`), `orchd` MCP in `~/.codex/config.toml`, per-repo
Claude trust, a writable state dir and `/tmp`.

The wizard lists every tool a full setup may use, so not every generic step is required by orchd. `orchd doctor`
decides what is required on macOS:

| doctor severity | macOS checks | Effect on the exit code |
| --- | --- | --- |
| required | git, Claude Code with `--bg`, Claude login, the Orch home with its `AGENTS.md` (after `orchd init`), Orch home trusted in Claude, the projects directory, the state dir, the private socket dir | a `fail` exits 1; an `unknown` (and no fail) exits 2 |
| optional | Codex CLI and login (needed only for Codex workers), Orch home trusted in Codex, Orch `.codex/config.toml` paths, Codex MCP launch entries, gh accounts (needed to open PRs), repos trusted in Claude, `orchd` on PATH, extra worker CLIs, the hidden `--messaging-socket-path` flag when `--help` does not list it | never worse than `warn` |

On native Windows doctor swaps the two providers: Codex and its app-server listener are required, Claude is optional.
Before `orchd init`, a missing Orch home is a `warn` that tells you to run init.

The projects directory is `ORCHD_PROJECTS`, else `projects_dir` in the personal `config.toml`, else the parent of an
orchd checkout under HOME, else `~/projects`. `orchd config show` prints the effective value. The Orch home
(`ORCHD_ORCH_HOME`) and state dir (`ORCHD_HOME`) have their own overrides; see the README's configuration table.

The wizard itself starts on any Python 3. Without `tomllib` (3.9/3.10, e.g. macOS `/usr/bin/python3`) the three checks that
read Codex TOML (Orch Codex trust, Orch `.codex/config.toml` paths, `orchd` MCP) report `unknown` with that reason; they are
never guessed with regex. Everything else runs unchanged. Run it with Python 3.11+ for the full result.

### Optional example profile

`--profile example` (doctor) and `--profile example|all` (wizard) add a fictional multi-account layout. The default
values are placeholders, not something your machine needs:

- several GitHub accounts, each with a gh login, its own SSH key and a `~/.ssh/config` host alias;
- per-repo git author emails;
- connector directories `email-tools`, `slack-tools` and `line-tools` under a connector root;
- extra worker CLIs (codegraph, rtk, gcloud, fastlane), reported only.

Point the profile at your own layout with environment variables instead of editing code:

| Setting | doctor | wizard |
| --- | --- | --- |
| GitHub accounts | `ORCHD_PROFILE_GH_ACCOUNTS` (comma-separated) | `ORCHD_PROFILE_ACCOUNTS_JSON`, e.g. `{"example-personal": ["github-personal", "id_ed25519_personal"]}` (account → host alias or null, key file) |
| SSH host aliases | `ORCHD_PROFILE_SSH_ALIASES` (comma-separated) | part of `ORCHD_PROFILE_ACCOUNTS_JSON` |
| Credential file directory | `ORCHD_PROFILE_CREDENTIAL_DIR` | not checked |
| Connector root | `ORCHD_PROFILE_CONNECTOR_ROOT` | `ORCHD_PROFILE_CONNECTOR_ROOT` |

Profile results never change doctor's exit code. What they prove is limited:

- doctor: a gh account passes when `gh auth status` lists it as logged in; an SSH alias passes when a matching `Host`
  line exists; SSH keys are counted by file name; credential files pass when a regular file matching the expected name
  pattern exists in the credential directory (contents never read); connector code passes when its directory exists.
- wizard: a connector directory that exists is reported `unknown`, never `pass`, because token presence is not
  checked; a missing one is `missing`. Per-repo git emails are always `unknown`.

A file or directory that exists is not a working login or a valid credential. Confirm each connector with its own
read-only status command.

## 3. What the wizard cannot verify (say so, don't guess)

- `--messaging-socket-path` is a hidden Claude flag; only `--bg` in `--help` is checked.
- Whether GitHub accepts an SSH key: needs a network call, which the wizard does not make. Operator runs `ssh -T <alias>`.
- Connector token presence: location differs per connector and secrets are never read. Status is `unknown`.
- Per-repo git author email: depends on which identity owns the repo.
- Login state passes only on affirmative output (`gh auth status` exit 0 with logged-in accounts; `claude auth status`
  `loggedIn: true`; `codex login status` saying logged in). A non-zero `gh` exit (e.g. one invalid token) is `unknown`.
  Credential files and `apiKey`-style config values are never read as proof or shown; of `~/.claude.json` and
  `~/.codex/config.toml` only trust flags and `mcp_servers.orchd` presence are kept. doctor is less strict for Codex:
  it treats `codex login status` exit 0 as logged in.
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
  `/home/example Space` is foreign to HOME `/home/example`. Unparseable TOML is `unknown`. A shell-like string such as
  `cd /home/example && y` is flagged whole (errs to `missing`, never to `pass`).
- `orchd doctor` (`orchd/doctor.py`) is not run or parsed by the wizard; that step is always `unknown` and
  Operator runs doctor directly (receipt row 2). In an older checkout without `orchd/doctor.py` the step says so.

Logins use each CLI's own browser flow; nobody pastes a token. Generate new SSH keys on the target machine and register
the public key. Do not copy private keys or export the old keychain.

## 4. Pilot receipts (Operator on the target machine, or a worker Operator authorises there)

| # | Who / where | Command | Receipt |
|---|---|---|---|
| 1 | Operator, target machine | `python3 scripts/setup-wizard.py --strict` (add `--profile all` only if you use the example profile) | exit 0, or each remaining non-pass step has a written reason |
| 2 | Operator, target machine | `bin/orchd doctor` | exit 0 (`required checks: all pass`); exit 1 = a required check failed, exit 2 = a required check unknown, neither is a pass |
| 3 | Operator, target machine | `ORCHD_HOME=$(mktemp -d)`, dispatch a task to a scratch repo | task id, worker running in `orchd list` |
| 4 | worker | `orchd ack`, `orchd progress`, `orchd report --status done` | all three visible in `orchd watch` |
| 5 | independent reviewer | read the pilot diff, write a review receipt | reviewer is not the pilot author |
| 6 | Operator | `orchd close <id>`; delete temp `ORCHD_HOME` and scratch repo | worktree gone, no leftovers |

Keep the outputs of 1–5 in your own records; Operator decides whether the machine is ready. If a step fails because of
orchd itself, open a public issue following [CONTRIBUTING.md](../CONTRIBUTING.md), with sanitized output and no
account names, host aliases or paths from your machine.
