# Bind a followup to its original worker

Local integrations can call `core.followup(..., expected=target)` with the complete
worker identity captured from the task. `store.ANSWER_TARGET_FIELDS` defines the
required fields. The native task delivery lock checks the target before acceptance.

The target stays on the native acceptance record, referenced by queued answers.
Every pending-answer read checks that target before delivery. A session, generation,
branch, worktree, model, or owner change holds the queue instead of delivering it to
a replacement worker. Retry refuses to stop the old worker while a bound followup
is pending. Deliver or explicitly reconcile the original queue before retrying.

Calls without `expected` keep the existing task-level followup behavior, including
queue transfer on retry. This optional argument is a local Python integration seam;
the MCP tool schema is unchanged. It restricts delivery and grants no new authority.

Verify with `python3 -m unittest tests.test_followup_targets tests.test_followup tests.test_auto_flush tests.test_retry`.
