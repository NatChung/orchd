# Run a committed workflow automatically

An ordered run fixes the workflow pin, repository choices, context strings, output bindings and reply origin once. The core then dispatches ready steps. Workers do their assigned work; they do not reconstruct or dispatch the next step. A worker may use local delegates within its task scope, but independent verification uses a separate task and session.

## Start from installed data

Install and bind a reviewed declarative package first. Declare its ordered plan as a [committed run template](workflow-run-templates.md). The Orch can inspect the installed template without reading a project checkout:

```sh
orchd workflow run-template example-workflow delivery --context '{"request":"Scoped input"}'
```

Start from an existing authenticated interface request owned by this Orch:

```sh
orchd workflow run start --workflow example-workflow --template delivery --context '{"request":"Scoped input"}' --request-id 42 --idempotency-key example-42 --orch-id example-orch
```

The equivalent Orch MCP call is `workflow_run_start(workflow, template, context, request_id, idempotency_key)`. It resolves the installed graph internally. Context cannot replace the graph, repositories or verification policy. `workflow_run_template(workflow, template, context)` only reads. Direct `--plan-file` start is an Operator boundary, not an Orch graph replacement interface.

The request fixes the interface, conversation and processing owner. An adapter token identifies transport; neither a token nor a template grants product writes or outward authority. This version requires an existing interface request record. It does not invent a Desktop or channel origin when that record is missing.

The same request and idempotency key with the same plan returns the existing run. A changed plan conflicts before new tasks. Later installations do not replace a running plan or its pinned bytes.

## Let the core advance

Start arms a detached core runner for this profile through `python -P -m orchd.cli`. The package has no `orchd.__main__`; launching it with `-m orchd` fails before reconciliation. `workflow run serve` is part of the installed daemon package, not a script copied into an Orch home. Its lifetime lock prevents two active service loops for the same database. A lost wake is recovered by periodic reconciliation. After a successful controlled Orch restart, the core ensures this same profile runner outside the owner-migration transaction; an existing lifetime lock prevents a duplicate loop. The stored run and workflow pin are retained.

A step input is an entire string from fixed context, a previously captured output, or an allowed prior-task metadata reference. The core preserves whitespace and Unicode. It captures output bytes once; subsequent changes to the producer file cannot silently alter downstream input. It checks the registered repository and pinned workflow before dispatch.

Workers use their normal ack, question and report protocol. A nonempty output declaration requires a regular JSON artifact inside that task's worktree:

```json
{"schema_version":"orchd-workflow-output/v1","values":{"instructions":"Original bytes\n"}}
```

Have the producing tool write the artifact, then report with `--output-file <task-relative-file>`. Missing, extra or non-string fields fail. A step declaring no outputs needs no artificial file; the core records an empty self-report envelope. Reported completion is distinct from verified acceptance.

A verification sidecar locks the author's real head and runs in another task before author cleanup. A textual PASS cannot replace the native verification receipt. Head movement invalidates the old lock. The reviewer materializes the intended target in its own clean review worktree before code review; native verify restores its previous head afterward. The reviewed SHA and the reviewer's current head are separate facts.

Producer worktrees stay available until the run reaches a terminal outcome, so later steps can read declared resources. Cleanup preserves workspaces that existing close rules consider unsafe to remove.

## Recover or stop explicitly

```sh
orchd workflow run show RUN_ID
orchd workflow run reconcile RUN_ID
orchd workflow run cancel RUN_ID --reason "The scoped test is finished" --orch-id example-orch
```

`workflow_run_show` reads persisted progress. `workflow_run_cancel` is an owned control action. Cancel fences future dispatch and closes only that run family, retaining evidence. Repeated cancellation does not create more work. Busy cleanup remains `cancelling` until the runner finishes it. If a final reply already exists, cancellation preserves that historical reply rather than rewriting it.

A crash after a worker may have started is not a reason to spawn another one. Ambiguous launch stays `needs_recovery` and notifies the original request. Normal startup and report reconciliation reuse the stored attempt identity.

A question pauses the run. A queued answer is not delivered authority. A subsequent valid report can resume only after the matching delivered answer; an unanswered question does not disappear merely because a worker reports done. This transport has one interim and one immutable final per request. A second distinct question can remain `origin_delivery_pending`; it must not silently switch to Desktop.

The engine owns replies for its request. Manual reply or dismiss cannot preempt its completion. Identified legacy messages on a wrong Desktop route are quarantined using stored task links, preserving their original content and historical delivery facts.

## Produce bounded improvements

A plan can declare one fixed `failure_steps` sequence. A real blocked result or failed native check creates at most one repair child. The child receives the original failure and captured parent output provenance as data. It cannot recursively create another repair sequence.

Engineering scripts, linters and skills belong to the worker's declared method or product repository. The core stores and enforces stage state; it does not execute plugin hooks or commands found in a failure record. A repair worker reproduces the failure, proposes a scoped fix and produces regression evidence. A different native verifier checks its candidate.

A successful repair ends at `needs_adoption`. It does not merge, publish, change a running method bundle, update a pin or grant more permissions. A missing basis or failed repair produces an honest blocked result at the original origin rather than leaving the root permanently repairing.

## Export only this run's evidence

```sh
orchd workflow run export --task-id TASK_ID --output <new-ignored-directory>
```

The export contains the frozen plan identity, real task mappings, scoped message rows, captured output values and a hash receipt. Worker identity and output location are checked before writing. It does not read other chats or invent the audit task's not-yet-reported result.

The legacy offline audit format models dependencies through close events. This engine defers close until the end, so the exporter keeps typed transition edges in `run.json` rather than fabricating close dependencies in the legacy plan. An external auditor must state which evidence kinds it actually supports. Export success is not workflow acceptance.
