## Summary

Slack 與 LINE 現在透過統一收件服務持久保存訊息，以跨 app 的 origin/claim ledger、防迴圈預算與撤回 tombstone 避免重複處理和正文復活；所有外送仍 hard-disabled。

```text
Slack Socket Mode ─→ durable receive ─┐
LINE signed bytes → local spool ─────┼→ origin / observation → claim + loop guard → owning Orch
LINE unsend ─────────────────────────┘  (immediate tombstone)
```

Closes #177
Part of #172

Proposed defaults, pending NatChung's confirmation: LINE direct receiver, single host, outbound disabled, PR2 retention with immediate unsend. The optional cloud edge receiver is **DEFERRED**. An offline direct host cannot receive webhooks; only its already accepted local spool can be replayed. No PR5 outbound or multi-host peer delivery is enabled.

## Evidence

- Before: reused the recorded `1c6c367` Python 3.12 baseline: 1813 tests, 55 failures, 443 errors, 16 skipped; earlier channel tests: 78, OK (1 skipped). The baseline was not rerun. The new tests first exposed missing modules, then immediate-unsend privacy and SDK callback thread failures; those failures were corrected. Final review also added and fixed a Slack delete-with-prior-files regression, then used the single permitted full-suite rerun.
- After: `python -m unittest discover -s tests -p 'test_channels_*.py' -v`: exit 0; 109 tests, `OK (skipped=1)`. This includes every earlier channel test and all four new files. Dedicated commands and the full-suite comparison are recorded below.
- Targeted: all exit 0: `test_channels_slack.py` 11 OK; `test_channels_line.py` 9 OK; `test_channels_loops.py` 7 OK; `test_channels_spool.py` 4 OK (commands: `python -m unittest discover -s tests -p 'test_channels_<name>.py' -v`).
- Full suite: final `python -m unittest discover -s tests`: exit 1; 1844 tests in 289.496s, `FAILED (failures=57, errors=443, skipped=16)`. The initial changed-code run was 1843 tests in 289.215s with the same 57/443/16 totals. Exactly one fix-and-rerun followed the newly discovered Slack delete-with-files regression. No additional full run or baseline rerun was performed.
- Name-set comparison: baseline 454 unique failing test/subtest names versus final 456: +3, -1. The added names are the known Windows `test_output_limits.ListOpenWireTest` timing/order cases `test_cursor_continues_after_leaving_attention_with_group_filter`, `test_new_tasks_wait_for_next_walk_and_walk_terminates`, and `test_one_huge_title_is_shortened_and_the_cursor_still_advances`. Inspection showed shuffled equal-time task IDs / a newly created task crossing a tied cursor boundary. Removed: `test_orch_inventory_lifecycle.LifecycleTest.test_mcp_text_keeps_complete_json_including_dead_unknown_and_archived` (baseline expected first row `dead`, got `codex`: another ordering assertion). All other outcome/name pairs are unchanged. These modules and their ordering paths are unmodified; the observed deltas are timing/order variation, with no failure attributable to PR4 found.
- Diff: 15 files changed, 1042 insertions(+), 8 deletions(-)
- Boundary: SDK imports remain inside adapters; LINE uses stdlib HMAC on raw bytes before parsing and only POST `/webhook`. Invalid signatures leave no durable event. No new MCP/CLI tools. Slack timestamps remain strings; history/thread windows commit only after durable receipts. 429 retains Retry-After for runner backoff. Missing scope and history gap leave checkpoints unchanged.
- Loop/privacy: two apps produce one claim; self/accepted-send echoes and unverified app proxying are suppressed. App allowlists cannot reset budgets or provenance. Claims and loop budgets commit/roll back together and survive restart. The four-turn lifetime cap remains paused after the five-minute window; hop cap is eight. Peer targets are disabled. Source ACL and no_reply zero-send tests pass. LINE unsend wins before/after the message and scrubs queued and canonical bodies. Lost spool ack replays without another claim; capacity exhaustion signals a gap instead of dropping pending events.
- Privacy: all fixtures, identities, text, secrets, clocks and platform transports are synthetic. No signatures or private payloads are logged; no credentials, production receiver addresses or machine configuration are committed. The spool file uses a protected Windows DACL / POSIX 0600 and SQLite secure deletion.
- Not verified: live Slack/LINE accounts, actual SDK reconnect/rate-limit behavior, production scopes, HTTPS deployment and production OS secret providers; no network/account tests or live sends were performed. `uv.lock` was intentionally not regenerated offline. See `docs/channels-slack-line.md` for lifecycle and rollback instructions. The receiver is a dedicated HTTP surface, not a new automatic listener or deployment.

```text
slack: exit 0
Ran 11 tests in 0.679s
OK

line: exit 0
Ran 9 tests in 0.864s
OK

loops: exit 0
Ran 7 tests in 0.421s
OK

spool: exit 0
Ran 4 tests in 0.157s
OK

all channel files: exit 0
Ran 109 tests in 7.873s
OK (skipped=1)

full suite: exit 1
Ran 1844 tests in 289.496s
FAILED (failures=57, errors=443, skipped=16)
```

## Merge Danger

Door: two-way — stop adapters and revert code while preserving the additive unified state and spool; received data cannot be discarded by restoring an old database.
Blast Radius: ingress
Rollback: stop socket/webhook admission and the runner, retain every origin/observation/claim/loop row and unacknowledged spool event, verify format compatibility before reverting, and never run a competing receiver. Tombstones remain authoritative; do not drop tables or resurrect unsent/unknown work.
Enablement: receive-only; outbound approval, claim and adapter sends remain disabled. No source, webhook, account authorization or listener is automatically enabled. Deployment and real sending require separate approval.
