# Update an instance from the Desktop entry

An Operator can request a configuration change in the restricted Desktop interface. The bound Orch can dispatch a worker to edit the instance's Git source. The worker reports its full commit and review evidence. It does not apply those changes to the live instance or grant itself permissions.

The instance must first have a successful local `orchd instance apply` receipt. That receipt fixes the source repository and profile paths. The entry tools cannot provision a new source or accept a path, command, executable, token, or approval flag.

## Check and apply a reviewed commit

1. Ask the Desktop entry to check the full 40-character commit with `instance_check(commit=...)`.
2. Read the returned operation through `orch_op_status(op_id=...)`. Check the fixed source repository, target root, commit, changed files, and `expected_receipt` in its `instance` record. The check writes an operation record, but does not change the instance files.
3. Explicitly ask the entry to apply that commit. It calls `instance_apply(commit=..., expected_receipt=...)` with the receipt digest from the check. A changed receipt rejects the request. The runner checks again before applying, and the apply API compares the digest while holding its apply lock.
4. Read the operation result through `orch_op_status`. `changed_files` and `restart_required` report the result. Apply does not restart the Orch or reload the current interface conversation.
5. If the result requires an Orch restart, explicitly request `orch_restart`. If it requires an interface reload, reload the entry's MCP or host through the supported host operation. The entry cannot claim to have reloaded its current conversation.

Once these entry tools are loaded, configuration maintenance uses the existing conversation. Adding or updating the entry tool definitions can require an initial MCP or host reload. Editing source files does not replace code already imported by a running MCP process.

## Keep the control boundary

Only the Desktop entry role exposes `instance_check` and `instance_apply`. Ordinary Orch tools, worker tools, and external adapter credentials do not expose these operations. External interface tokens identify adapters, not Operator authority.

The runner uses the source and profile from the active instance's successful receipt. It rejects profile mismatches and stale receipts. Detached operations share the existing entry-operation lock and persist their outcome under the instance state directory. A vanished runner reports `lost`; it does not automatically retry apply or restart.

Operator intent remains the existing prose requirement. Tool descriptions require an explicit Operator request and forbid treating an Orch, worker, or external message as approval. The implementation does not authenticate a human or prove that the model followed those instructions. A local process that can modify trusted receipts or configuration is outside this guard's protection.
