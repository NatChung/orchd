# Install verification

This page separates what CI proves about a packaged install from what only a person on a real
machine can prove. A green CI run is **not** end-to-end acceptance.

## Installed package vs. source checkout

| | Installed package (`uv tool install …`) | Source checkout (`git clone …`) |
| --- | --- | --- |
| Entry point | `orchd` on the uv tool bin dir (`uv tool update-shell` adds it to PATH) | `bin/orchd` in the checkout |
| Contents | the `orchd` Python package only, including `templates/` (with the hidden `home/.codex` and `interface/.codex` configs) and `worker_bin/gh` | everything in the repo: `scripts/`, `tests/`, `docs/` |
| Setup wizard | **not installed**; `scripts/setup-wizard.py` is a checkout script | `python3 scripts/setup-wizard.py` |
| Update | `orchd upgrade` (follows the source uv recorded) | update the checkout itself, then reinstall from it if you also use the tool |

Read-only machine checks from an installed package use `orchd doctor`. The setup wizard prints the
same kind of plan from a checkout; neither installs software, signs in, or writes configuration.

## What CI checks

`.github/workflows/ci.yml`:

- Linux: `python -m unittest discover -s tests` and `python -m compileall -q orchd scripts tests`
  on Python 3.11 and 3.13.
- Linux and native Windows: `uv build`, then `scripts/check-package.py dist`, which
  1. asserts the wheel contains `orchd/templates/home/.codex/config.toml`,
     `orchd/templates/interface/.codex/config.toml` and `orchd/worker_bin/gh`;
  2. installs that wheel with `uv tool install` into throwaway `HOME`/`USERPROFILE`, `UV_TOOL_DIR`,
     `UV_TOOL_BIN_DIR`, `UV_CACHE_DIR`, `XDG_*` and `ORCHD_*` directories;
  3. puts only the isolated bin dir in front of `PATH`, checks `orchd` resolves there, and runs
     `orchd --help` outside any checkout;
  4. runs `orchd doctor --json --no-input` before init (see below).

The Windows unit-test subset runs in `.github/workflows/native-portability.yml`; the full suite
assumes POSIX process and socket behavior. CI never starts a daemon, Orch, worker or MCP client
session, never runs `orchd init`, and uses no provider or GitHub account.

You can run the same package check locally:

```sh
uv build --out-dir /tmp/orchd-dist
python3 -I scripts/check-package.py /tmp/orchd-dist
```

## Expected `doctor` result before sign-in and init

On a machine without signed-in provider CLIs and before `orchd init`, `orchd doctor` is expected to
**exit 1** (`required checks: FAILED`). That is correct, not a packaging failure:

- `orch home` is `warn`: "… not set up yet", with the fix "Run `orchd init`".
- The required provider check fails: `claude CLI`/`claude login` on macOS/Linux, `codex CLI`/`codex login`
  on native Windows (the other provider is optional and only warns).
- `projects dir` fails when the configured projects directory does not exist yet.
- Optional integrations (gh, codegraph, rtk …) only warn.

Exit 2 means no required check failed but at least one could not be determined; it is not a pass.
`--json` and `--no-input` never prompt and never save settings, and doctor writes nothing under the
Orch root. After `uv tool update-shell`, open a new terminal and confirm the PATH update with
`command -v orchd` (PowerShell: `Get-Command orchd`); it should point into the uv tool bin dir
(`uv tool dir --bin`), not a checkout's `bin/`.

## Manual new-machine acceptance (not CI)

Run this on a clean machine or a fresh user account with your own accounts. Record the date, OS,
orchd commit, and provider CLI versions; post only redacted results.

1. Install prerequisites: Python 3.11+, Git, uv, and the provider CLIs (Claude Code; Codex CLI on
   Windows or for Codex workers). On macOS/Linux, install tmux.
2. Sign in through each provider CLI yourself. orchd never handles credentials.
3. `uv tool install git+https://github.com/NatChung/orchd.git`, then `uv tool update-shell`; open a
   new terminal and confirm `orchd` resolves from the uv tool bin dir.
4. `orchd doctor`: before init, expect the results above minus the login failures.
5. `orchd init`; accept the Claude trust prompt for the Orch home; set the projects directory
   (`orchd config set projects-dir …`) to a directory containing a throwaway Git repository.
6. `orchd doctor` again: required checks should all pass (exit 0). Investigate any `????` line.
7. `orchd binding` to start and bind an Orch, then `orchd list`.
8. Dispatch one read-only worker task against the throwaway repository (see the README dispatch
   example), wait for its report with `orchd inbox`, and check the report's evidence yourself.
9. Close or finish the task, confirm the worker's worktree and processes are cleaned up, and stop the
   Orch (`orchd orch stop ID`).
10. Optional: open the interface folder in Codex Desktop and request the bound Orch's status.

Only a completed run of this list, with its evidence checked, counts as new-machine acceptance.
