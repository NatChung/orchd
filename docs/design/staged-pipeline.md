# Staged delivery pipeline

Status: proposed design. [#119](https://github.com/NatChung/orchd/issues/119) covers only the minimal preflight and PR-creation checks described in implementation phase 1. [#121](https://github.com/NatChung/orchd/issues/121) tracks phases 2–4 and the full pipeline, with #119 as its first phase. This document describes new behavior, not an installed capability. The tracker remains the source of scope and acceptance. Orchd stores execution state and evidence, keyed by the canonical issue reference. This design does not change issue content or labels.

## Scope by issue

| Section or phase | Issue scope |
| --- | --- |
| Definition and version | #121; #119 uses the existing Hub pin for its minimal checks. |
| Artifacts and handoff | #121; #119 records preflight results and baseline. |
| Gates and state | #121; #119 enforces preflight and checks before `gh pr create` without staged state. |
| Interfaces and recovery | #121. |
| Test plan | #119 covers its preflight and PR-creation checks; #121 covers the staged pipeline and remaining cases. |
| Phase 1 | #119: preflight and checks before `gh pr create`. |
| Phase 2 | #121. |
| Phase 3 | #121. |
| Phase 4 | #121. |

## Definition and version

The existing `[delivery_flows]` mapping selects the Hub checkout for a product repo, as specified in [Worker delivery flow configuration](../delivery-flow-config.md). Orchd fetches that Hub and resolves `origin/HEAD` to a full commit, using the existing fetch cache and failure rules. The guide is `docs/development/delivery.md`; the machine definition is `docs/development/delivery.pipeline.json` at the same commit. The guide can describe additional human decisions, but cannot silently override the machine definition. There is no manual definition override. Orchd reads the exact committed blob, validates its schema and supported version, and stores the blob and SHA-256 in its DB at pipeline creation. It also records the Git commit and path. Failing closed when the pipeline Hub fetch fails is new behavior: the existing delivery flow reports fetch failure in the worker brief. A failed fetch, missing blob, unsupported version, or invalid definition fails closed; local dirty files never supply the pinned bytes. Existing runs keep their pinned bytes; a new run picks up a new reviewed Hub version. A future explicit migration must validate and audit both definitions and preserve accepted evidence; editing Hub HEAD never changes a running pipeline.

**Per-repo enablement.** The definition carries a required top-level `repos` array of unique, non-empty orchd repo names (the names `dispatch` takes, not Hub or GitHub names). `[delivery_flows]` still selects the Hub; `repos` selects which of the repos that map to it are gated. A repo that is not listed is treated exactly like a repo with no definition: orchd returns no pipeline for it, so preflight and the `gh pr create` check pass through unchanged. A definition without `repos` (the pre-enablement format) is rejected with an error asking for it, and malformed `repos` is rejected too; this fails closed, so no repo is gated implicitly by an old file. `repos: []` enables nothing. Checks are shared by all listed repos; per-repo overrides are deliberately not supported yet.

**Explicit base branch.** `dispatch` and `dispatch_batch` (CLI/MCP entry points through `core.dispatch`) accept an optional `base_branch`. When given, orchd validates the name, fetches that branch from `origin` and refuses the dispatch, before any task exists, if it is not there. The worktree is cut from `origin/<base_branch>`, `tasks.base_branch` records it, and `tasks.base` is its tip commit, so preflight (which runs on `base`) and the PR gate (which pins `--base` to `tasks.base_branch`) both follow it. This lets a repo that targets `release-X.Y.Z` be enabled. When omitted, behavior is unchanged: `origin/HEAD` is used.

Example, abbreviated only in the command arguments; all stage names and required skills are shown:

```json
{
  "schema_version": 1,
  "id": "delivery",
  "version": "1.0.0",
  "stages": [
    {"id":"plan", "skills":["how","why","architect","blast-radius","show-me-your-work"], "codegraph":true, "model_rule":"configured", "artifact_schema":"plan-v1", "decision_log_schema":"decision-log-v1.schema.json"},
    {"id":"implement", "skills":["tdd","test-behavior-not-implementation","fix-root-causes","laziness-protocol","prove-it-works","show-me-your-work"], "model_rule":"configured", "artifact_schema":"implement-v1", "decision_log_schema":"decision-log-v1.schema.json"},
    {"id":"verify", "skills":["interrogate","blast-radius","prove-it-works","show-me-your-work"], "model_rule":"different-vendor-from-implement", "artifact_schema":"verify-v1", "decision_log_schema":"decision-log-v1.schema.json"},
    {"id":"review_pr", "skills":["Opening a PR","technical-writing","unslop","no-comments","show-me-your-work"], "model_rule":"different-vendor-from-author", "artifact_schema":"review_pr-v1", "decision_log_schema":"decision-log-v1.schema.json"}
  ],
  "checks": {
    "preflight": {"argv":["./scripts/preflight"], "cwd":".", "timeout_seconds":300, "allowed_exit_codes":[0], "env_allowlist":["PATH"], "required":true},
    "pr": {"argv":["./scripts/check-pr"], "cwd":".", "timeout_seconds":120, "allowed_exit_codes":[0], "env_allowlist":["PATH"], "required":true}
  }
}
```

The real definition specifies argv as arrays, working directory, timeout, allowed exit codes, environment allowlist, and input contract for each check. Orchd executes only commands from the pinned, trusted Hub definition, without a shell, in the designated task worktree. It records command, base and head, exit status, and bounded output in immutable evidence. Secrets stay out of artifacts. Unknown keys and unsupported check types fail validation. A missing required check blocks; no configured pipeline preserves existing behavior. Repo-native rules and existing authority still apply. The Hub owner must define safe checks and pin their dependencies; a command's exit status is not proof of its semantic claims.

The model rule resolves the actual launched model using `models.alias_for` and `models.catalog()`: the catalog merges `models.DEFAULTS` with `[models.<alias>]` in orchd config, and its validated `vendor` is `codex` or `claude`. Record the resolved alias, model ID and vendor at launch, not the quota or the requested alias alone. Although `models.vendor_of` falls back to a `gpt-` prefix for unknown IDs, pipeline independence requires a catalog match and fails closed on unknown or ambiguous IDs. Config changes do not rewrite recorded attempt identities. The verify provider must differ from the actual implementation worker provider, including retries. If identity is unknown or no eligible provider is available, dispatch blocks with a reason; there is no same-provider fallback. Each stage has one worker attempt at a time. A retry creates an audited attempt, and changed implementation evidence invalidates downstream stages. Skills are instructions for the worker, not executable proof of their use. The present worker-flow adapter restricts skills and prohibits cross-vendor review; implementation must revise that adapter and worker brief deliberately before enabling this pipeline, without relaxing outward-action or merge authority.

## Artifacts and handoff

Each attempt submits one UTF-8 JSON manifest plus immutable evidence files. Paths are relative to an orchd-owned artifact directory, never arbitrary host paths or symlinks. Orchd copies and hashes submitted bytes before validation. A `show-me-your-work` decision log is a required handoff file: each row records decision, reason, evidence reference, and result. The next worker receives the pinned definition, accepted manifests, evidence hashes, decision log, and exact source revision. Artifacts survive worktree cleanup and are not dependent on a worker's local files.

All manifests require these fields, with no unknown fields: `schema_version` (1), `pipeline_id`, `issue_ref`, `stage`, `attempt`, `task_id`, `worker_model`, `provider`, `base_sha`, `head_sha`, `definition_sha256`, `inputs` (prior accepted manifest hashes), `documents` (nonempty array of `{uri, revision, sha256}`), `decision_log` (`{path, sha256}`), and `evidence` (nonempty array of `{id, path, sha256, media_type}`). Revisions are full Git object IDs for repository documents; non-Git sources use an immutable version and content digest. Each referenced file must exist, match its digest, and be within size limits. Stage-specific fields below are required in addition. Structured command records contain `argv`, `cwd`, `exit_code`, `output_evidence_id`, `source_sha`, and `started_at`. They refer to captured output, not worker-supplied success text alone.

| Stage | Required stage-specific fields and meaning |
| --- | --- |
| `plan` | `scope` and `acceptance` arrays; `affected_paths`, `risks` with disposition and proof planned; `codegraph` with index counts, queries and results evidence; `preflight` command record and baseline results. Record relevant architecture, issue, Hub and skill documents in `documents`. |
| `implement` | `red` and `green` command records, with test identity and source revisions; `baseline` command records; `changes` with commit SHA; `root_cause` and `behavior_coverage`; `proof` command records. Red must fail for the intended behavior before the fix, green must pass after it. |
| `verify` | `implementation_manifest_sha256`; `independent_provider`; `verification` command records; `blast_radius` risks with actual proof and result; `mutation` with removal patch evidence, isolated revision, failing command record, and restored passing command record. The removal must remove the fix, not damage the test or environment. |
| `review_pr` | `reviewed_head_sha`, `review_findings` with disposition, `pr_check` command record, `pr` with repository, number, URL, base and head, and mandatory `review_record` containing verdict, reviewer identity, provider and reviewed SHA. The author submits preparation evidence first; the separate review task supplies this record before the stage can be accepted. Record PR preparation and review evidence separately. |

For a stage with no meaningful code query or test, the definition may define a narrow, explicit alternative artifact schema. A worker cannot waive a field in its own manifest. Code-change runs use the four schemas above. A failed test, unavailable skill, missing source, or unsupported exception is recorded as a blocker, not converted to a pass. Automated validation can check hashes, exit codes, sequence, identities and command provenance; it cannot establish that a test is meaningful, a mutation removes the fix, or a review is sound. The independent worker and reviewer assess those claims and record their reasoning.

### Complete manifest examples

These examples show the complete v1 object shape for each stage. Repeated digest characters, IDs, commands and URLs are placeholders; a valid submission must replace them with real matching bytes and provenance. `inputs` contains accepted prior-manifest SHA-256 strings in stage order. The plan has no inputs. Every array shown with one entry may contain additional entries. Each stage requires all fields shown for it, and no unknown fields. A command record may add `test_identity` for red and green. Command `cwd` is relative to the task worktree. A stage validator checks types and required fields against these shapes, then checks the cross-field, file, identity and gate rules above. The examples alone do not establish that those checks pass.

Each stage definition names `decision-log-v1.schema.json`, the shared schema for a decision-log entry shown below. The decision log is UTF-8 JSON Lines. Each line is one object matching this JSON Schema (draft 2020-12); `evidence_id` references an ID in the manifest's evidence array. An empty log is invalid.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"required":["decision","reason","evidence_id","result"],"properties":{"decision":{"type":"string","minLength":1},"reason":{"type":"string","minLength":1},"evidence_id":{"type":"string","minLength":1},"result":{"type":"string","minLength":1}}}
```

#### `plan-v1`

```json
{
  "schema_version": 1,
  "pipeline_id": "pipe-1",
  "issue_ref": "github:Example/product#1",
  "stage": "plan",
  "attempt": 1,
  "task_id": "task-1",
  "worker_model": "gpt-example",
  "provider": "codex",
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "definition_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "inputs": [],
  "documents": [
    {
      "uri": "docs/development/delivery.md",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  ],
  "decision_log": {
    "path": "decisions.jsonl",
    "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "evidence": [
    {
      "id": "output",
      "path": "output.txt",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "media_type": "text/plain"
    }
  ],
  "scope": [
    "Fix behavior"
  ],
  "acceptance": [
    "Regression passes"
  ],
  "affected_paths": [
    "orchd/example.py"
  ],
  "risks": [
    {
      "risk": "Existing callers",
      "disposition": "test",
      "proof_planned": "caller test"
    }
  ],
  "codegraph": {
    "files": 123,
    "nodes": 4650,
    "queries": [
      "explore example"
    ],
    "results_evidence_id": "output"
  },
  "preflight": {
    "argv": [
      "python",
      "-m",
      "unittest"
    ],
    "cwd": ".",
    "exit_code": 0,
    "output_evidence_id": "output",
    "source_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "started_at": "2026-01-01T00:00:00Z"
  }
}
```

#### `implement-v1`

```json
{
  "schema_version": 1,
  "pipeline_id": "pipe-1",
  "issue_ref": "github:Example/product#1",
  "stage": "implement",
  "attempt": 1,
  "task_id": "task-2",
  "worker_model": "gpt-example",
  "provider": "codex",
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "definition_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "inputs": [
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  ],
  "documents": [
    {
      "uri": "docs/development/delivery.md",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  ],
  "decision_log": {
    "path": "decisions.jsonl",
    "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "evidence": [
    {
      "id": "output",
      "path": "output.txt",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "media_type": "text/plain"
    }
  ],
  "red": {
    "argv": [
      "python",
      "-m",
      "unittest"
    ],
    "cwd": ".",
    "exit_code": 1,
    "output_evidence_id": "output",
    "source_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "started_at": "2026-01-01T00:00:00Z",
    "test_identity": "regression"
  },
  "green": {
    "argv": [
      "python",
      "-m",
      "unittest"
    ],
    "cwd": ".",
    "exit_code": 0,
    "output_evidence_id": "output",
    "source_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "started_at": "2026-01-01T00:00:00Z",
    "test_identity": "regression"
  },
  "baseline": [
    {
      "argv": [
        "python",
        "-m",
        "unittest"
      ],
      "cwd": ".",
      "exit_code": 0,
      "output_evidence_id": "output",
      "source_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "started_at": "2026-01-01T00:00:00Z"
    }
  ],
  "changes": {
    "commit_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "root_cause": "Missing validation",
  "behavior_coverage": [
    "Reject invalid input"
  ],
  "proof": [
    {
      "argv": [
        "python",
        "-m",
        "unittest"
      ],
      "cwd": ".",
      "exit_code": 0,
      "output_evidence_id": "output",
      "source_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "started_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

#### `verify-v1`

```json
{
  "schema_version": 1,
  "pipeline_id": "pipe-1",
  "issue_ref": "github:Example/product#1",
  "stage": "verify",
  "attempt": 1,
  "task_id": "task-3",
  "worker_model": "claude-example",
  "provider": "claude",
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "definition_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "inputs": [
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  ],
  "documents": [
    {
      "uri": "docs/development/delivery.md",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  ],
  "decision_log": {
    "path": "decisions.jsonl",
    "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "evidence": [
    {
      "id": "output",
      "path": "output.txt",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "media_type": "text/plain"
    }
  ],
  "implementation_manifest_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "independent_provider": "claude",
  "verification": [
    {
      "argv": [
        "python",
        "-m",
        "unittest"
      ],
      "cwd": ".",
      "exit_code": 0,
      "output_evidence_id": "output",
      "source_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "started_at": "2026-01-01T00:00:00Z"
    }
  ],
  "blast_radius": [
    {
      "risk": "Existing callers",
      "proof": "caller test",
      "result": "pass"
    }
  ],
  "mutation": {
    "removal_patch_evidence_id": "output",
    "isolated_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "failing_command": {
      "argv": [
        "python",
        "-m",
        "unittest"
      ],
      "cwd": ".",
      "exit_code": 1,
      "output_evidence_id": "output",
      "source_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "started_at": "2026-01-01T00:00:00Z"
    },
    "restored_command": {
      "argv": [
        "python",
        "-m",
        "unittest"
      ],
      "cwd": ".",
      "exit_code": 0,
      "output_evidence_id": "output",
      "source_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "started_at": "2026-01-01T00:00:00Z"
    }
  }
}
```

#### `review_pr-v1`

```json
{
  "schema_version": 1,
  "pipeline_id": "pipe-1",
  "issue_ref": "github:Example/product#1",
  "stage": "review_pr",
  "attempt": 1,
  "task_id": "task-4",
  "worker_model": "gpt-example",
  "provider": "codex",
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "definition_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "inputs": [
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  ],
  "documents": [
    {
      "uri": "docs/development/delivery.md",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  ],
  "decision_log": {
    "path": "decisions.jsonl",
    "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "evidence": [
    {
      "id": "output",
      "path": "output.txt",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "media_type": "text/plain"
    }
  ],
  "reviewed_head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "review_findings": [
    {
      "finding": "No findings",
      "disposition": "pass"
    }
  ],
  "pr_check": {
    "argv": [
      "python",
      "-m",
      "unittest"
    ],
    "cwd": ".",
    "exit_code": 0,
    "output_evidence_id": "output",
    "source_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "started_at": "2026-01-01T00:00:00Z"
  },
  "pr": {
    "repository": "Example/product",
    "number": 1,
    "url": "https://github.com/Example/product/pull/1",
    "base": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "review_record": {
    "verdict": "PASS",
    "reviewer_identity": "task-5",
    "provider": "claude",
    "reviewed_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}
```

## Gates and state

Orchd owns transitions. `report done`, a file on disk, and a PR do not advance a stage. The advance command validates the pinned definition and current accepted inputs, runs required validators, verifies evidence and source identities, and reports every failed requirement. It atomically accepts the manifest and advances exactly one stage. The caller supplies the expected pipeline revision and an idempotency key. A stale revision fails without changes; replay of the same key and payload returns the original result, while reuse with different bytes fails. Concurrent submissions cannot both advance.

The plan gate runs `checks.preflight` against the pinned base in the task worktree and requires its passing command record in the plan manifest. The implementation gate requires that accepted plan preflight and the red and green evidence; it does not run a second preflight. `checks.pr` runs against the candidate head immediately before PR creation in `review_pr`; its passing record belongs to that stage. The verification gate requires a different provider, passing independent checks, and a failing removal test followed by a restored pass. The review gate requires the PR check, correct base and head, required existing labels, and required PR body sections, including TDD red and green and blast radius, as defined by the Hub. Before PR creation, a managed `gh pr create` wrapper checks the current pinned run and exact PR inputs. A missing or failed check blocks creation and explains the failure. Enforcement requires routing every supported worker PR creation path through the wrapper and denying direct GitHub write credentials for bypass paths; a wrapper alone is not a security boundary. Existing preview and authorization rules still govern outward actions.

The four-stage worker prepares the PR. Within the `review_pr` gate, an independent review task uses a different catalog vendor from the author and cannot be the author. It reads the diff, latest base, tests and current head, records a comment verdict with the reviewed SHA, and merges only when explicitly tasked to review and merge, only on PASS, and with `--match-head-commit`. It never bypasses protection. A head or base change after verification or review invalidates affected evidence and requires the appropriate stages to run again. The pipeline cannot declare delivery complete on an authored PR alone. An open PR awaiting review is visible as the next action; an authorized review-only task stops after its comment.

Proposed persistent tables, with foreign keys and transactional migrations. Extend `store.connect_readonly` compatibility checks to require the new tables and their required columns alongside the existing tasks checks. A missing or old schema raises `DatabaseUnavailable` without changing the source database; a normal writable command migrates it:

- `pipelines(id, issue_ref, repo, owner_orch, definition_blob, definition_sha256, hub_commit, hub_path, stage, status, revision, created_at, updated_at)`. Validate `issue_ref` with `issue_links.validate_ref` before insertion and lookup; it accepts `github:owner/repo#N` or `jira:KEY-N` with positive N. `owner_orch` references the owning Orch. A partial unique constraint permits one active pipeline per canonical issue and repo. Status is `active`, `blocked`, `awaiting_review`, `complete`, or `cancelled`. The partial unique index covers `active`, `blocked`, and `awaiting_review`; `complete` and `cancelled` are terminal and excluded.
- `stage_attempts(id, pipeline_id, stage, attempt, task_id, model, provider, status, base_sha, head_sha, manifest_sha256, created_at)`. `(pipeline_id, stage, attempt)` and `task_id` are unique. Attempt status is `reserved`, `running`, `submitted`, `accepted`, `failed`, `superseded`, or `cancelled`. The partial unique index for one live attempt per pipeline covers `reserved`, `running`, and `submitted`.
- `pipeline_stages(pipeline_id, stage, status, accepted_attempt_id)` has one row per stage, unique on `(pipeline_id, stage)`. Status is `pending`, `running`, `blocked`, `accepted`, `superseded`, or `cancelled`. Rework resets the earliest affected row to `pending`, marks later accepted rows `superseded`, and resets them to `pending` when reached.
- `pipeline_artifacts(id, attempt_id, kind, content_sha256, storage_key, byte_length)` stores durable content-addressed bytes. A manifest references these IDs and digests.
- `pipeline_events(id, pipeline_id, revision, actor, action, from_state, to_state, payload, idempotency_key, created_at)` is append-only. Uniqueness on pipeline and idempotency key supports replay. Rejections are recorded without accepting an artifact or changing stage.

The stage sequence is `plan → implement → verify → review_pr`; `awaiting_review` is a pipeline status within `review_pr`, not a fifth stage. After the review gate and any required authorized merge, the pipeline becomes `complete`. Each stage begins `pending`, becomes `running` on managed dispatch, and becomes `accepted` only through its gate. Failures leave the stage in place with a visible blocker; retry creates a new attempt. Rework returns to the earliest affected stage and marks later accepted attempts superseded, retaining their history. Cancellation is explicit and audited. `complete` requires the review outcome and any separately authorized merge outcome required by the pinned definition; an unmerged PR can remain `awaiting_review`. Pipeline state is separate from task lifecycle and external issue state.

## Interfaces and recovery

Proposed CLI and equivalent MCP operations use the same service and validation:

- `orchd pipeline start --issue REF --repo REPO` and `pipeline_start` pin the definition and create the first pending stage.
- `orchd pipeline dispatch ID --expected-revision N` and `pipeline_dispatch` select the allowed model, reserve one attempt, and create its worker task with `issue_ref` and prior artifacts. Dispatch must persist the attempt before spawning and reconcile a failed or uncertain spawn before retrying, so a transport error cannot create duplicate workers.
- `orchd pipeline submit ID --task TASK --manifest PATH --expected-revision N --idempotency-key KEY` and `pipeline_submit` copy evidence and perform the gate. No worker can submit for another attempt.
- `orchd pipeline show ID` and `pipeline_show` return stage, status, revision, attempt, blockers, accepted evidence, and exact next command. `pipeline list` and `pipeline_list` include pipelines with no open task. `list_open` keeps its existing output by default; an explicit `include_pipeline=true` option adds pipeline ID, stage, status and next action to related tasks; a separate pipeline list prevents closed tasks from hiding unfinished tickets.
- `orchd pipeline rework` and `pipeline_rework` create audited attempts or invalidate downstream evidence with an explicit reason and expected revision. They do not bypass gates. CLI and MCP expose equivalent operations and errors.

Extend `core.adopt --from` to transfer all nonterminal pipelines owned by the source Orch, including those without open tasks, in the same transaction as their task ownership and adoption events. For explicit task adoption, transfer the associated pipeline and all its live tasks together or reject the partial move. Lock pipeline ownership with the related tasks; reject partial or stale ownership changes. Extend `orch_restart.restart` inventory, dry-run and adopt condition to include owned nonterminal pipelines even when there are no open tasks or intake receipts. A committed transfer remains authoritative if a notification fails; expose that failure for reconciliation. After Orch restart or ownership handoff, the new Orch reads pipeline list and show from DB, then reconciles live workers, pending questions, uncertain deliveries, artifact availability and PR state. It does not infer progress from conversation or automatically repeat an uncertain external action. Missing or corrupt artifact bytes block advancement and identify the required recovery. DB transactions make a crash before commit leave the prior state and a crash after commit expose the accepted revision; content-addressed orphan files can be collected only after reference checks. Backup and restore must include both DB and artifact store.

Existing dispatch remains available for repositories without a pipeline. For repositories with the pipeline rollout enabled, ordinary `core.dispatch` and all CLI and MCP entry points reject dispatch unless it carries an internal, validated pipeline attempt reservation; there is no bypass override. Managed dispatch links each stage task to the same issue reference and passes the pinned flow, not a mutable copy of Hub HEAD. Existing task `close` still safely stops a worker and handles its worktree; it neither advances nor deletes pipeline evidence. A failed launch, closed task, or worker report remains visible and requires an explicit retry. Issue closure remains independent of task closure and pipeline completion. The existing tracker workflow and review-worker authority remain in force.

## Test plan

- Validate real and malformed definitions, unsupported versions, failed fetches or missing pins, command allowlists, and legacy repos with no configuration. Test old `connect_readonly` schemas, invalid issue references, ordinary dispatch rejection, and unchanged default `list_open` output. Assert that only configured repos are gated.
- Submit each stage with missing fields, mismatched hashes, wrong inputs, wrong SHA, missing output, failing checks, or an unavailable skill. Assert rejection, useful reasons, unchanged revision, and no downstream dispatch. Verify the PR wrapper blocks missing label, wrong base, missing body sections and failed check; test direct-write credential isolation as well.
- Run a behavior test through red and green. Remove the fix in an isolated checkout and show the same test fails, then restore and pass. Reject fabricated order, wrong source revisions, test-damaging mutation, and same-provider verification. Test unknown provider and no alternate provider.
- Race two submissions and dispatches, replay an idempotency key, and reuse a key with different bytes. Inject crashes before and after artifact copy, DB commit, worker spawn, and external PR request. Assert a single transition, no duplicate worker or PR, and explicit reconciliation for uncertain outcomes.
- Restart Orch at each stage, with a worker running, blocked, closed, and awaiting review. Adopt the tasks into another Orch and verify list and show recover the same stage, evidence, blocker and next action. Delete or corrupt evidence and confirm advancement blocks. Restore a DB and matching artifact backup and repeat.
- Change implementation head, base, definition HEAD, or PR head after verification. Assert pinned definitions remain stable, affected evidence is superseded, and stale review cannot merge. Exercise review-only, explicit review-and-merge, failed checks, branch protection, and task close without advancing the pipeline.

## Implementation order

1. **#119, independently deliverable:** add configured preflight and the checks immediately before `gh pr create`, with actionable failures and tests. Use the existing Hub pin and preserve behavior for unconfigured repos. This phase does not require pipeline tables or staged dispatch; it does not claim a wrapper alone prevents credential bypass.
2. **#121:** add pinned pipeline definitions, DB migrations, artifact storage, schemas, events, and read-only list and show, including old-DB compatibility and owner recovery. Ship as disabled capability with existing dispatch unchanged.
3. **#121:** add opt-in managed dispatch, provider selection, stage gates, idempotency, rework, dispatch bypass prevention, and crash reconciliation. Ship for configured repos with tests and an explicit rollout switch.
4. **#121:** link the phase 1 checks to stage gates and enforce the PR creation boundary with credential isolation. Add independent cross-vendor review linkage, exact-head invalidation, and the adapter changes; pilot before broader rollout. Until this phase is enabled, do not claim the complete pipeline is enforced.
