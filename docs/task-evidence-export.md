# Export a standalone task's evidence

`workflow run export` requires an ordered run. For a normal stage dispatch, use:

```sh
orchd task export --task-id example-task --request-id 42 \
  --input-key question --output .local/evidence-001 --check
```

The output must be a new ignored directory inside that task's retained worktree,
with an existing parent. Worker identity and Orch ownership checks still apply.
The command reads a private read-only DB/WAL snapshot and does not migrate the DB,
dispatch work, modify a workflow, send replies or change request state.

The caller explicitly selects the request ID. The exporter checks that the request
and task currently belong to the same Orch. **This is not a captured origin binding:**
it does not prove that the selected request caused this dispatch. Do not guess a
request from timestamps, titles or matching text. Omit `--request-id` when unknown;
the export will report missing comparison evidence. Cross-Orch selection is refused.

The files retain the original `request.txt`, dispatch-time workflow input in
`dispatch-input.txt`, actual worker instructions and completion criteria in
`task.json`, workflow pin/inputs in `workflow.json`, and scoped task messages in
`core.jsonl`. `comparison.json` and `request-to-dispatch.diff` compare the original
request to the selected input, without confusing workflow boilerplate with lost
requirements. More than one input requires `--input-key`; a missing/invalid input,
missing dispatch, or unknown request is never treated as identical.
The comparison also records whether the complete request text occurs in the
stored worker instructions. This containment check is separate from input
equality: inspect `task.json` for changes in worker instructions or completion
criteria, and do not treat either check as semantic acceptance.

With `--check`, identical UTF-8 bytes exit 0, different bytes exit 1, and unavailable
comparison evidence exits 2. A difference is not proof of an incorrect decision:
an authorized clarification can change wording. Without `--check`, exit 0 means
only that the export was written. The receipt hashes every exported evidence file.
Existing exports are never overwritten. Keep these files private; they can contain
request text, task questions and replies.

Core app events retain item IDs/timestamps, not the worker's commands and outputs.
Use the exported task `session_id` to locate the original worker log for a separate,
authorized inspection. No hidden reasoning or unrelated chats are read by this
exporter. A receipt is not a signature, semantic acceptance, or proof of compliance.
