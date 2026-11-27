# Verify the Desktop round trip

The test opens a fresh Desktop chat bound to a test Claude Orch and asks it to
dispatch one Claude worker in a synthetic local Git repository. It checks the
worker's question, the numbered reply and its link to the worker answer, the
worker report, and the final assistant output in the original Desktop rollout.
The output identifies the failed boundary or records the verified thread, Orch,
task, question, and answer IDs.

## Run on a dedicated macOS desktop

Use a separate macOS test account or machine with ChatGPT Desktop open in the
logged-in GUI session and Codex and Claude available on PATH. Sign into a
dedicated Claude profile and set `ORCHD_DESKTOP_E2E_CLAUDE_CONFIG` to its absolute
directory. The script rejects the default Claude profile. If Desktop uses a
non-default Codex home, set the matching `CODEX_HOME`. The script adds trust
entries for the synthetic Orch home and Git fixture only to the dedicated Claude
profile. Desktop project trust, if requested, applies to the new fixture project.

Run `python -m pip install .`, then `python scripts/desktop_roundtrip.py`. Open the
printed Interface folder in Desktop and enter the printed messages in the same
chat. Set `ORCHD_DESKTOP_E2E_DRIVER` to an absolute, executable driver path to
automate those UI steps on a dedicated runner. The driver receives three
arguments, `open|send`, the new Interface project path, and the text. For
`open`, it must open a new chat with the project Interface permissions and
request binding status. For `send`, it must submit the exact text in that same
Desktop chat. Keep the chat open through the final receipt. CLI-created chats
are outside this contract.

The smoke generates a new root, DB, orchd settings, test Git origin and scope for
each invocation. It stops only the Orch and worker registered in that DB.
Desktop rollouts remain in the dedicated test account.

## Run in CI

Label the dedicated macOS runner `orchd-desktop-e2e` and start it from the GUI
account with the environment above. Run **Desktop round trip** from main in
Actions, or call `.github/workflows/desktop-roundtrip.yml` from a trusted
main-branch release workflow. Without a configured driver, an operator must be
present at the runner's Desktop to follow the log instructions. Once the driver
works, set the repository Actions variable `ORCHD_DESKTOP_E2E_ENABLED` to `true`
to run automatically on pushes to main. The push job requires the driver.
No provider credentials belong in repository files. PR CI runs the separate unattended
stale-owner reconnect regression.
