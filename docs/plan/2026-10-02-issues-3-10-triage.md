# Issues #3/#4/#5/#6/#7/#8/#10 triage (2026-10-02, base main 8b86770)

Status: triage only. 0 of 7 implemented by this task.

## Current evidence

| # | Done | Not done |
|---|---|---|
| 3 | nothing | doctor, wizard, Mac mini pilot |
| 4 | nothing | dispatch verify fields, lock, `orchd verify`, report check |
| 5 | nothing | followup, retry, MCP tools, orch-repo config/AGENTS |
| 6 | `orchd summary` (watch.summary: workers, models, questions, peak, worker output tokens); worker usage events at close | stats per Orch x task_type, Orch tokens, cost, verify/retry metrics |
| 7 | nothing; inbox already follows current `tasks.orch_thread` | adopt CLI, event, wake, read-but-unhandled visibility |
| 8 | Nat decided 2026-10-02 (via Orch): auto review then auto merge | brief + Orch AGENTS change |
| 10 | Codex worker runtime + Sol (43a67cb, 3534858) | Luna model, L->M->H escalation, pilot, stats column |

Facts checked: claude 2.1.287, codex-cli 0.159.3; codegraph/rtk/gcloud/fastlane/gh present; ~/.ssh/config has the 3 aliases;
prod DB (read-only): 13 codex orchs (id = thread id), 6 claude orchs (all with session_id).
NatChung/orchd is private on a free plan: branch protection API 403, so no required-review rule exists there.
Orch MCP approval list (`~/projects/orch/.codex/config.toml`) and Orch AGENTS.md live in NatChung/orch.

## Shared contract (PROPOSAL, not approved)

Required by issues: #4 store exit/tail/SHA + script hash in DB; #5 retry event from->to; #7 adopt event; #6 reads them; decisions v2 #9 append-only events.
Below is my suggestion only.


- New facts are events in `messages` (like dispatch/answer/close/usage). No new tables.
- Kinds: `verify_lock {paths, hashes:{path:blob}, sha, command}`, `verify {exit, tail, sha, dirty, hash_ok}`,
  `retry {from, to, old_job, old_session}`, `followup {text}`, `adopt {from, to}`.
- `ORCH_KINDS` owner: #7. `TASK_COLUMNS` owner: #4 (rebase against 9964338e/#12 if both touch store).
- Hands off: `list_open` (a13338ff, a16107f0), `_wake` signature (PR15), `close` (PR14), `worker_brief` until ceafef0b merges.
- Orch-repo changes (approval_mode for new tools, AGENTS.md rules) are separate tasks in NatChung/orch.

## Per ticket

### #3 doctor — dispatch now (Part A)
Files: new `orchd/doctor.py`, `tests/test_doctor.py`, `doctor` subparser in `bin/orchd`; wizard script.
Doctor is read-only; wizard only guides Nat's steps. Runner failure = unknown, not pass.
done_when: tests per check (pass/fail/unknown); doctor output on this machine green or known gaps; unittest + diff --check; PR.
Part B (mini setup + pilot): needs Nat at the mini.

### #7 adopt — dispatch now
Files: `core.adopt`, `store.move_task_orch`, `adopt` subparser (CLI only), `ORCH_KINDS += adopt`.
Target registered and not stopped; explicit ids or `--from <orch>`; adopt event + one wake; old-owner-alive behavior pending Q-#7 (issue silent).
Adopt inbox item carries current status + last question/report (covers read-but-unhandled).
done_when: new inbox shows old unread + adopt item; later wakes go to new Orch only; rejects closed/unknown/stopped target;
read_at, worktree, branch untouched; PR.

### #5 retry — dispatch now; followup after 9964338e (#12)
Files: `core.retry`, MCP tool `retry`, tests. Not answer/list_open/close.
Reject closed, missing worktree; model rule pending Q-#5 (issue only says 'stronger'). Record old session usage before stop. Same worktree/branch; dirty kept;
vendor switch branches trust/socket/job fields; prompt = task message + retry note + last progress/report; retry event;
on start failure mark failed and keep the worktree.
followup: Claude = send_uds + event; Codex = #12 delivery function.
done_when: tests for dirty kept, both vendor directions, rejects, usage-before-swap; PR. Orch config/AGENTS separately.

### #6 stats — dispatch now
Files: new `orchd/stats.py`, `stats --since` subparser, `tests/test_stats.py`. Not watch.summary.
Orch tokens: claude_usage(orchs.session_id); codex_usage(orch_id). `--since`: Claude per-line timestamps; Codex cumulative diff.
USD = estimate from in-code price table, null when unknown.
Metrics per Orch x task_type: tasks, completed, questions/answers, rework (nat split out), cost/completed, Orch tokens;
verify/retry/followup/escalation from contract events (0/null until #4/#5).
done_when: fixtures, one test per metric, windowing test; PR.

### #4 verify (lock is tamper-evident only: workers same uid, full perms, DB/source writable; Q-#4 asked) — dispatch after ceafef0b merges
Dispatch gets optional `verify` (command) and `manual_checks`; worker declares lock paths.
Worker commits script, runs `orchd verify <id>` (expected fail, recorded). Orch MCP `lock_verify(task_id)` stores
git blob hashes of declared paths at HEAD + command (verify_lock event); relock only by Orch, as a new event.
`orchd verify` after lock: compare hashes, run command in worktree, store exit/tail/HEAD/dirty.
`report --status done`: flag (not reject) when no lock, hash changed, last verify sha != HEAD, failed, or dirty;
flag goes into the report's inbox item.
done_when: tests for hash lock and SHA check (edit locked file -> flagged; commit after verify -> flagged); PR;
pilot via real Orch afterwards. Orch config/AGENTS separately.

### #8 review policy — dispatch after ceafef0b merges (shares worker_brief)
Decision (Nat, 2026-10-02): an independent review worker checks the PR against done_when, required tests,
and current head vs main; if all pass it merges without asking again; the author never merges;
problems or missing dependencies -> no merge.
Change: brief says record the verdict with `gh pr review --comment` (PASS or problems + head SHA checked),
never `--approve`/`--request-changes`; brief keeps 'merge only when the task explicitly says review-and-merge', batch authorization goes in task instructions (same account as the author, GitHub rejects); merge with
`gh pr merge --match-head-commit <sha>`; never bypass branch protection (`--admin`) or self-approve; if the repo
requires an approving review, report blocked instead.
Files: `core.worker_brief` text + test on brief text; Orch AGENTS.md review rule (orch repo, separate).
done_when: brief test asserts comment/no-approve/no-admin/match-head; PR.

### #10 Luna — asked Nat (keep deferred vs opt-in now)
Remaining: `luna` in MODELS with max effort, escalation rule (needs #4 + #5), pilot + stats (needs #6 + baseline).

Dispatched by Orch: #3A = 62f300b4, #6 = f38f7fae (Codex window baseline = last counter before since).

## Order
Now in parallel: #3A, #7, #5-retry, #6.
After ceafef0b: #4, then #8 (or #8 first; both edit worker_brief, sequence them).
After 9964338e: #5-followup.
After #4 + #5 + #6 + baseline: #10.
Nat: #3B on the mini.

## Update after Nat answers (2026-10-02)
- #4: Nat chose hash lock + independent re-run. Author runs never count as accepted. Verifier task V (dispatch `verifies=A`)
  re-runs at the reviewed SHA; orchd recomputes lock hashes from git objects; accepted_pass needs independent event at
  A's reported HEAD, hash_ok, exit 0, clean; later commits -> stale. Orch confirms against V's real report.
  Fixture cases a-h in progress 4. Not tamperproof (same uid). Dispatch after #8 brief PR merges.
- #8: Orch dispatching sonnet for the brief change.
- PRs in review: #21 answer queue (#12), #22 doctor (#3A), #23 stats (#6).
- Pending Nat: #5 model rule, #7 alive/unknown owner force, #10 Luna defer.
