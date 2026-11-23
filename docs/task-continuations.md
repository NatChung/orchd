# Authenticated task origins and retained continuations

An instance may retain the original author while a review is still open. This is an opt-in resource lifecycle: a retained task is not closed, and a lease is not product, repository or outward-message authority.

## Opt in new author dispatches

In the protected instance configuration:

```toml
[continuation]
repositories = ["example-repository"]
task_types = ["code", "docs", "ops"]
require_origin = true
owner = "review-feedback"
```

Existing tasks are unaffected. The configured repository list is an instance grant for this capability; untrusted user text is not a repository grant. An adapter must also check its own current project and repository authorization before activating any external route.

Dispatch with `origin_request_id` and a stable `dispatch_key` (CLI: `--origin-request-id` and `--dispatch-key`). The native owner verifies the request belongs to the calling Orch and its interface is enabled. A new origin must still be active. Task, immutable origin, dispatch key and retention lease are saved together before creating a worktree or launching a worker. Repeating the same key and intent returns the existing task. An interrupted launch stays attached to that task; replay does not launch a second worker. The first leased Claude input has a durable fixed frame UUID before its socket write; an uncertain initial send retains the original worktree/session and receipt rather than running legacy pre-launch cleanup.

Origin validation failure creates a sanitized native incident. `task_origins.list_dispatch_incidents(con)` returns stable incident IDs, codes, owner/repository, intent hash and status, without request text or task instructions. A corrected successful dispatch with the same original intent resolves that incident, preserving its history. Consumers without a verified source must use their configured administrative destination, never guess a conversation.

`task_origins.for_task(con, task_id)` returns a captured interface origin, or the existing frozen ordered-workflow origin. Repository paths are the native registered dispatch paths; provenance does not replace the adapter's original scope and permission checks. The first native worker identity is frozen by `task_continuations.bind_original_worker`. A changed worker is refused.

## Close and release

While held, `core.close` returns `closed: false`, `deferred: "continuation"` and the lease ID. It keeps the existing worker and worktree and retains the requested outcome/rating. Repeated close is idempotent; conflicting explicit outcomes are rejected. Ordered workflow completion records `cleanup_deferred` rather than calling the author closed. Held leases prevent automatic provider replacement and explicit `retry`; they never change an original worker's model or session.

The trusted Operator consumer calls:

```python
lease = task_continuations.bind_original_worker(con, task_id)
result = task_continuations.release(
    con, runtime, lease['id'], lease['worker_digest'], reason='review_closed')
```

The consumer must first establish that its external review has closed, and must not release while a delivery is unknown or a continuation is active. Release checks the original identity under the native delivery lock, refuses active/uncertain turns, pending queue input and unresolved provider deliveries, then finishes the recorded native close once. A managed Claude frame being submitted does not prove it finished; its exact executing UUID and terminal parent chain are required before cleanup. The same guard covers the initial leased input. Existing dirty-worktree preservation still applies. If no close was requested, release removes the retention but does not invent a close outcome. A failed close stays `release_pending` and can resume the same native cleanup. Historical closed tasks are never reopened. Dismissing or replying to the original interface request does not cancel retention. An explicit native workflow cancellation or task close with outcome `abandoned` revokes retention and completes its existing stop/cleanup, recording that distinct cancellation reason.

## Idempotent followup and exact evidence

A trusted adapter calls `core.followup(..., expected=complete_original_identity, idempotency_key=operation_id)`. Keyed responses include `acceptance_id`, `task_id` and `session_id`. `task_continuations.followup_for_key` is a lookup-only recovery API; it does not queue when the key is absent. Different input or identity for a used key is rejected. Accepted but unconfirmed writes remain uncertain; they are never replaced by another task.

`task_continuations.followup_evidence` reads the original acceptance and native provider history. It returns the original instruction, exact composed input, provider receipt, and a terminal public assistant text only after proving the specific executed input and final:

- Codex app-server: original generation/session, native queue message IDs, exact wrapped input and the original delivery client ID or committed turn ID; only that completed turn's final answer is returned.
- Claude: frame UUID and composed batch are reserved before the socket write. A definite connection failure may keep the same operation queued; an uncertain write is not resent. Only the original executing user UUID and its terminal assistant parent chain prove completion. A queued command alone is not execution, and a later unrelated final is ignored.

Completed evidence normalizes `delivery.state` to `completed`, includes the actual turn/final-message ID and keeps the original `journal_state`. It does not rewrite an unknown journal merely because an adapter read the result. A proven failed/interrupted provider turn returns `status: failed` and `provider_turn_failed`; a definite pre-turn rejection remains pending with `provider_rejected` while its existing queue can safely flush. Rejected attempts remain audit evidence and do not count as duplicate execution. These APIs never call dispatch, retry, adopt or resume. Unsupported or missing evidence remains pending/unsupported; consumers must not substitute the latest transcript answer.

`task_continuations.provider_evidence(con, runtime, task_id, provider_delivery_id, expected=identity)` exposes the same precise Claude proof for initial and ordinary retained inputs. Reading evidence never changes a delivery journal. After positive completion, the trusted consumer may call `reconcile_followup(..., acceptance_id, expected=identity)` or `reconcile_provider(..., provider_delivery_id, expected=identity)` to finish the original queue and receipt bookkeeping. Reconciliation rechecks the original identity under the delivery lock, records its proof once, and never sends input or starts a worker.

Consumers parse their own public result format. The core does not know an external adapter's envelope, Slack or GitHub identity mapping, message routes, or authorization rules.
