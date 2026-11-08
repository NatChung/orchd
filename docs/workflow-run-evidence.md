# Ordered run evidence export

`workflow run export` writes one mapped task's run evidence into a new ignored directory inside that task's worktree. The exporter does not execute inputs, inspect referenced artifact paths, or modify task state.

Existing `orchd-run-export/v1` fields, `auditplan.json`, and close-based audit semantics remain unchanged. The additive evidence capability is identified by `evidence_schema_version: "orchd-run-evidence/v1"` in `run.json` and `receipt.json`.

## Files and references

| File | Content |
|---|---|
| `run.json` | Frozen plan, state, attempts, expected transitions, and companion references |
| `auditplan.json` | Existing task/stage mapping and input digests, without invented close dependencies |
| `core.jsonl` | Scoped dispatch, ack, report, close, verify_lock, and verification messages |
| `run-events.jsonl` | Actual `workflow_run_events` rows for this run |
| `outputs/<attempt_id>.json` | Original captured output bytes, when the snapshot contains an output |
| `receipt.json` | SHA256 digests for every exported data file, plus counts |

`run.json.run_events_file` is `run-events.jsonl`; `core_messages_file` is `core.jsonl`. Each event has `id`, `run_id`, `kind`, `task_id`, `source_message_id`, `body`, and `created_at`. `body` retains the stored JSON text. Event IDs preserve native stream order; they are not a replacement for source message links.

Each existing attempt gains these fields:

- `attempt_id`: the stored attempt identity.
- `task_verifies`: the actual task row's `verifies` value, or null when unavailable.
- `dispatch_spec`: selected stored dispatch fields: repo, stage, task_type, outputs, verifies, and workflow metadata. It omits rendered instructions and completion prose. Declared workflow input strings remain available for digest comparison.
- `output_raw_file`: a fixed companion filename, or null when no output was captured.

The existing `output_values`, `output_digest`, and `output_raw_digest` fields remain. Raw output files come directly from the captured database BLOB. Re-serializing `output_values` does not reproduce the original bytes. A mismatch between the stored output digest and the raw bytes is preserved for the consumer to report, rather than corrected during export.

The receipt adds `run_event_count`, `last_run_event_id`, and `output_count`. `files` includes the events and every raw output file. A missing event stream produces an empty JSONL file and zero count. Missing outputs produce null references, not invented empty envelopes.

## Snapshot and verification boundaries

Run metadata, attempts, task verification links, core messages, native events, and captured output BLOBs come from the same SQLite read savepoint. Concurrent commits cannot mix a newer event stream with older captured outputs. An existing caller transaction remains open and is not committed by export.

Native lock and verification records retain their actual message bodies and evidence in `core.jsonl`. Consumers must check the matching author, verifier, lock ID, SHA, path hashes, command, and result conditions. The stored `passed` flag alone is not proof of success. Export does not read Git HEAD or establish current freshness.

Only the run containing the exporting task is selected. `parent_run_id` and recovery event references remain metadata. They do not automatically include another run's messages, events, or outputs. A separate explicit export is needed for that run.

## Consumer checks

An ordered auditor can reconstruct edges from `frozen_plan` and verify report/capture, step_succeeded, reserved/dispatch, and native verification evidence. The exporter does not itself pronounce those gates passed. The legacy audit plan deliberately omits close dependencies because producer cleanup occurs after downstream work.

Before reading companions, consumers must verify regular files, reject symlinks and traversal, enforce paths under the export directory, and check receipt hashes. Output values are opaque declarations, not permission to open arbitrary paths. Receipt hashes detect changed exported bytes; they are not a signature, semantic acceptance, or evidence that method instructions were followed.

## Checks

```sh
python3 -m unittest tests.test_workflow_run_export
```
