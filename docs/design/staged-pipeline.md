# Staged delivery pipeline

Status: proposed design for [#119](https://github.com/NatChung/orchd/issues/119). This document describes new behavior, not an installed capability. The tracker remains the source of scope and acceptance. Orchd stores execution state and evidence, keyed by the canonical issue reference. This design does not change issue content or labels.

## Definition and version

A Hub owns a machine-readable `delivery.pipeline.json` next to its delivery guide. The guide can describe additional human decisions, but cannot silently override the machine definition. A repo route identifies the Hub Git repository, path, and full commit. Orchd reads the exact committed blob, validates its schema and supported version, and stores the blob and SHA-256 in its DB at pipeline creation. It also records the Git commit and path. Dirty files, missing blobs, unsupported versions, and invalid definitions fail closed. Existing runs keep their pinned bytes; a new run picks up a new reviewed Hub version. A future explicit migration must validate and audit both definitions and preserve accepted evidence; editing Hub HEAD never changes a running pipeline.

Example, abbreviated only in the command arguments; all stage names and required skills are shown:

```json
{
  "schema_version": 1,
  "id": "delivery",
  "version": "1.0.0",
  "stages": [
    {"id":"plan", "skills":["how","why","architect","blast-radius"], "codegraph":true, "model_rule":"configured", "artifact_schema":"plan-v1"},
    {"id":"implement", "skills":["tdd","test-behavior-not-implementation","fix-root-causes","laziness-protocol","prove-it-works"], "model_rule":"configured", "artifact_schema":"implement-v1"},
    {"id":"verify", "skills":["interrogate","blast-radius","prove-it-works"], "model_rule":"different-vendor-from-implement", "artifact_schema":"verify-v1"},
    {"id":"review_pr", "skills":["Opening a PR","technical-writing","unslop","no-comments"], "model_rule":"configured", "artifact_schema":"review-pr-v1"}
  ],
  "checks": {
    "preflight": {"argv":["./scripts/preflight"], "required":true},
    "pr": {"argv":["./scripts/check-pr"], "required":true}
  }
}
```

The real definition specifies argv as arrays, working directory, timeout, allowed exit codes, environment allowlist, and input contract for each check. Orchd executes only commands from the pinned, trusted Hub definition, without a shell, in the designated task worktree. It records command, base and head, exit status, and bounded output in immutable evidence. Secrets stay out of artifacts. Unknown keys and unsupported check types fail validation. A missing required check blocks; no configured pipeline preserves existing behavior. Repo-native rules and existing authority still apply. The Hub owner must define safe checks and pin their dependencies; a command's exit status is not proof of its semantic claims.

The model rule resolves configured model aliases to a provider identity from trusted configuration. The verify provider must differ from the actual implementation worker provider, including retries. If identity is unknown or no eligible provider is available, dispatch blocks with a reason; there is no same-provider fallback. Each stage has one worker attempt at a time. A retry creates an audited attempt, and changed implementation evidence invalidates downstream stages. Skills are instructions for the worker, not executable proof of their use. The present worker-flow adapter restricts skills and prohibits cross-vendor review; implementation must revise that adapter and worker brief deliberately before enabling this pipeline, without relaxing outward-action or merge authority.

## Artifacts and handoff

Each attempt submits one UTF-8 JSON manifest plus immutable evidence files. Paths are relative to an orchd-owned artifact directory, never arbitrary host paths or symlinks. Orchd copies and hashes submitted bytes before validation. A `show-me-your-work` decision log is a required handoff file: each row records decision, reason, evidence reference, and result. The next worker receives the pinned definition, accepted manifests, evidence hashes, decision log, and exact source revision. Artifacts survive worktree cleanup and are not dependent on a worker's local files.

All manifests require these fields, with no unknown fields: `schema_version` (1), `pipeline_id`, `issue_ref`, `stage`, `attempt`, `task_id`, `worker_model`, `provider`, `base_sha`, `head_sha`, `definition_sha256`, `inputs` (prior accepted manifest hashes), `documents` (nonempty array of `{uri, revision, sha256}`), `decision_log` (`{path, sha256}`), and `evidence` (nonempty array of `{id, path, sha256, media_type}`). Revisions are full Git object IDs for repository documents; non-Git sources use an immutable version and content digest. Each referenced file must exist, match its digest, and be within size limits. Stage-specific fields below are required in addition. Structured command records contain `argv`, `cwd`, `exit_code`, `output_evidence_id`, `source_sha`, and `started_at`. They refer to captured output, not worker-supplied success text alone.

| Stage | Required stage-specific fields and meaning |
| --- | --- |
| `plan` | `scope` and `acceptance` arrays; `affected_paths`, `risks` with disposition and proof planned; `codegraph` with index counts, queries and results evidence; `preflight` command record and baseline results. Record relevant architecture, issue, Hub and skill documents in `documents`. |
| `implement` | `red` and `green` command records, with test identity and source revisions; `baseline` command records; `changes` with commit SHA; `root_cause` and `behavior_coverage`; `proof` command records. Red must fail for the intended behavior before the fix, green must pass after it. |
| `verify` | `implementation_manifest_sha256`; `independent_provider`; `verification` command records; `blast_radius` risks with actual proof and result; `mutation` with removal patch evidence, isolated revision, failing command record, and restored passing command record. The removal must remove the fix, not damage the test or environment. |
| `review_pr` | `reviewed_head_sha`, `review_findings` with disposition, `pr_check` command record, `pr` with repository, number, URL, base and head, and `review_record` containing verdict, reviewer identity and reviewed SHA when applicable. Record PR preparation and review evidence separately. |

For a stage with no meaningful code query or test, the definition may define a narrow, explicit alternative artifact schema. A worker cannot waive a field in its own manifest. Code-change runs use the four schemas above. A failed test, unavailable skill, missing source, or unsupported exception is recorded as a blocker, not converted to a pass. Automated validation can check hashes, exit codes, sequence, identities and command provenance; it cannot establish that a test is meaningful, a mutation removes the fix, or a review is sound. The independent worker and reviewer assess those claims and record their reasoning.

## Gates and state

Orchd owns transitions. `report done`, a file on disk, and a PR do not advance a stage. The advance command validates the pinned definition and current accepted inputs, runs required validators, verifies evidence and source identities, and reports every failed requirement. It atomically accepts the manifest and advances exactly one stage. The caller supplies the expected pipeline revision and an idempotency key. A stale revision fails without changes; replay of the same key and payload returns the original result, while reuse with different bytes fails. Concurrent submissions cannot both advance.

The implementation gate requires the red and green evidence and the configured preflight pass. The verification gate requires a different provider, passing independent checks, and a failing removal test followed by a restored pass. The review gate requires the PR check, correct base and head, required existing labels, and required PR body sections, including TDD red and green and blast radius, as defined by the Hub. Before PR creation, a managed `gh pr create` wrapper checks the current pinned run and exact PR inputs. A missing or failed check blocks creation and explains the failure. Enforcement requires routing every supported worker PR creation path through the wrapper and denying direct GitHub write credentials for bypass paths; a wrapper alone is not a security boundary. Existing preview and authorization rules still govern outward actions.

The four-stage worker prepares the PR. The existing review worker is a separate task and cannot be the author. It reads the diff, latest base, tests and current head, records a comment verdict with the reviewed SHA, and merges only when explicitly tasked to review and merge, only on PASS, and with `--match-head-commit`. It never bypasses protection. A head or base change after verification or review invalidates affected evidence and requires the appropriate stages to run again. The pipeline cannot declare delivery complete on an authored PR alone. An open PR awaiting review is visible as the next action; an authorized review-only task stops after its comment.

Proposed persistent tables, with foreign keys and transactional migrations:

- `pipelines(id, issue_ref, repo, definition_blob, definition_sha256, hub_commit, hub_path, stage, status, revision, created_at, updated_at)`. A partial unique constraint permits one active pipeline per canonical issue and repo. Status is `active`, `blocked`, `awaiting_review`, `complete`, or `cancelled`.
- `stage_attempts(id, pipeline_id, stage, attempt, task_id, model, provider, status, base_sha, head_sha, manifest_sha256, created_at)`. `(pipeline_id, stage, attempt)` and `task_id` are unique. One live attempt per pipeline is enforced.
- `pipeline_artifacts(id, attempt_id, kind, content_sha256, storage_key, byte_length)` stores durable content-addressed bytes. A manifest references these IDs and digests.
- `pipeline_events(id, pipeline_id, revision, actor, action, from_state, to_state, payload, idempotency_key, created_at)` is append-only. Uniqueness on pipeline and idempotency key supports replay. Rejections are recorded without accepting an artifact or changing stage.

State transitions are `plan → implement → verify → review_pr → awaiting_review → complete`. Each stage begins `pending`, becomes `running` on managed dispatch, and becomes `accepted` only through its gate. Failures leave the stage in place with a visible blocker; retry creates a new attempt. Rework returns to the earliest affected stage and marks later accepted attempts superseded, retaining their history. Cancellation is explicit and audited. `complete` requires the review outcome and any separately authorized merge outcome required by the pinned definition; an unmerged PR can remain `awaiting_review`. Pipeline state is separate from task lifecycle and external issue state.

## Interfaces and recovery

Proposed CLI and equivalent MCP operations use the same service and validation:

- `orchd pipeline start --issue REF --repo REPO --definition COMMIT:PATH` and `pipeline_start` pin the definition and create the first pending stage.
- `orchd pipeline dispatch ID --expected-revision N` and `pipeline_dispatch` select the allowed model, reserve one attempt, and create its worker task with `issue_ref` and prior artifacts. Dispatch must persist the attempt before spawning and reconcile a failed or uncertain spawn before retrying, so a transport error cannot create duplicate workers.
- `orchd pipeline submit ID --task TASK --manifest PATH --expected-revision N --idempotency-key KEY` and `pipeline_submit` copy evidence and perform the gate. No worker can submit for another attempt.
- `orchd pipeline show ID` and `pipeline_show` return stage, status, revision, attempt, blockers, accepted evidence, and exact next command. `pipeline list` and `pipeline_list` include pipelines with no open task. `list_open` adds pipeline ID, stage, status and next action to related tasks; a separate pipeline list prevents closed tasks from hiding unfinished tickets.
- `pipeline retry` and `pipeline_rework` create audited attempts or invalidate downstream evidence with an explicit reason and expected revision. They do not bypass gates. CLI and MCP expose equivalent operations and errors.

After Orch restart or ownership handoff, the new Orch reads pipeline list and show from DB, then reconciles live workers, pending questions, uncertain deliveries, artifact availability and PR state. It does not infer progress from conversation or automatically repeat an uncertain external action. Missing or corrupt artifact bytes block advancement and identify the required recovery. DB transactions make a crash before commit leave the prior state and a crash after commit expose the accepted revision; content-addressed orphan files can be collected only after reference checks. Backup and restore must include both DB and artifact store.

Existing dispatch remains available for repositories without a pipeline. For configured repositories, managed dispatch links each stage task to the same issue reference and passes the pinned flow, not a mutable copy of Hub HEAD. Existing task `close` still safely stops a worker and handles its worktree; it neither advances nor deletes pipeline evidence. A failed launch, closed task, or worker report remains visible and requires an explicit retry. Issue closure remains independent of task closure and pipeline completion. The existing tracker workflow and review-worker authority remain in force.

## Test plan

- Validate real and malformed definitions, unsupported versions, dirty or missing pins, command allowlists, and legacy repos with no configuration. Assert that only configured repos are gated.
- Submit each stage with missing fields, mismatched hashes, wrong inputs, wrong SHA, missing output, failing checks, or an unavailable skill. Assert rejection, useful reasons, unchanged revision, and no downstream dispatch. Verify the PR wrapper blocks missing label, wrong base, missing body sections and failed check; test direct-write credential isolation as well.
- Run a behavior test through red and green. Remove the fix in an isolated checkout and show the same test fails, then restore and pass. Reject fabricated order, wrong source revisions, test-damaging mutation, and same-provider verification. Test unknown provider and no alternate provider.
- Race two submissions and dispatches, replay an idempotency key, and reuse a key with different bytes. Inject crashes before and after artifact copy, DB commit, worker spawn, and external PR request. Assert a single transition, no duplicate worker or PR, and explicit reconciliation for uncertain outcomes.
- Restart Orch at each stage, with a worker running, blocked, closed, and awaiting review. Adopt the tasks into another Orch and verify list and show recover the same stage, evidence, blocker and next action. Delete or corrupt evidence and confirm advancement blocks. Restore a DB and matching artifact backup and repeat.
- Change implementation head, base, definition HEAD, or PR head after verification. Assert pinned definitions remain stable, affected evidence is superseded, and stale review cannot merge. Exercise review-only, explicit review-and-merge, failed checks, branch protection, and task close without advancing the pipeline.

## Implementation order

1. Agree on Hub ownership, trusted command execution, schema versions, artifact retention, and credential isolation. Revise the adapter and worker brief to permit the specified skills and independent provider while retaining existing authority.
2. Add DB migrations, pinned definition loading, artifact storage, validation, events, and read-only list and show. Preserve legacy behavior and test restart and migration before dispatch integration.
3. Add managed stage dispatch, provider selection, gate submissions, idempotency, rework, and crash reconciliation. Add stage schema validators and end-to-end red, green, and removal evidence tests.
4. Integrate preflight and PR checks and enforce the PR creation boundary. Add review-worker linkage and exact-head invalidation, then pilot on an opted-in repo before broader rollout.
