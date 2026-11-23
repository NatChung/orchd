# Reload an idle Orch in its original session

`python -m orchd.orch_reload` is an Operator-only maintenance entry point. Run it
under the new deployment's Python interpreter, with the intended profile's
`ORCHD_*` paths. It exposes no worker MCP tool. It resumes the original Claude
Orch ID, session and model; author task rows, generations and owners are untouched.

Supply a new canonical UUID operation ID and the exact observed Orch ID, session,
job and resolved model:

```sh
python -m orchd.orch_reload reload --state /srv/example/state.db \
  --operation 00000000-0000-4000-8000-000000000001 --orch example-owner \
  --expected-session original-session --expected-job original-job \
  --expected-model example-model
```

The old provider job must positively identify the original session and Orch cwd,
with live PID, `status=idle` and `state=blocked`. This is a maintenance eligibility
check, not a claim that a conversation has completed its work. The command takes
the same cross-process Orch lock as native wake/revive. A durable operation and
internal `stopped_at` marker are committed before requesting a stop. Old versions
of native revive already refuse that marker, preventing a competing resume while
the reload is pending. The marker is restored to NULL only by a matching operation
CAS after fresh provider evidence identifies the replacement's exact original
session/cwd, live process, and listening socket. A concurrent human stop wins.

The replacement's MCP configuration is written by `Runtime.start_orch`, using the
Python interpreter executing this module. Set profile-local entry point/config
paths before calling; this command does not rewrite global configuration.

Inspect an operation with `status --state DB --operation UUID`. A noncomplete
reload exits 2. `stop_pending` can be repeated with the same exact identity and
operation ID; it requests the same old job stop again. It needs positively retired
old-job metadata or the saved process identity proof described below, together
with a nonlistening socket and no surviving original session. Provider absence
alone does not prove this stop or a spawn outcome. No SQLite write transaction spans process calls.

`spawn_pending` means acceptance might be unknown (including a process crash).
Repeating reload **never starts another process** in this phase. Inspect the
provider's launch receipt and pass its new job ID to:

```sh
python -m orchd.orch_reload reconcile --state /srv/example/state.db \
  --operation 00000000-0000-4000-8000-000000000001 --candidate-job replacement-job
```

Reconciliation never spawns. It excludes every provider job present before the
operation and requires a single fresh, live candidate for the exact original
session/cwd. A persisted receipt can be reconciled without `--candidate-job`.
Missing or ambiguous evidence leaves the Orch quiesced. A returned different
session is held without stopping the unproven returned job; it may belong to a
concurrent author even if it was absent from the initial provider snapshot.
There is intentionally no automatic cancellation or blind retry after uncertain
spawn acceptance: preserve the operation and inspect provider evidence. Never
clear the marker by hand to force progress.

## Providers that remove stopped jobs

New operations persist the provider's complete pre-stop job descriptor and its
PID's actual OS creation time before the stop. A removed provider job is never
sufficient by itself. Stop confirmation additionally requires a fresh successful
job listing from the same executable/profile with no live or unidentified
original-session process, a nonlistening original socket, and OS evidence that
the recorded PID is absent or its creation time differs from the saved OS birth.
Permission errors and unchanged birth remain held. The stop proof is committed
with `spawn_pending` before launch.

`startedAt` in the provider descriptor is a provider timestamp in milliseconds;
it is preserved as provenance and is **never compared with OS creation time**.
OS birth is captured separately in seconds. An older operation without that OS
birth can only use positive `NoSuchProcess`, never inferred PID reuse.

For an already pending older operation, an Operator may attach its exact
pre-stop snapshot, containing `orch` and `job` objects, without editing SQLite:

```sh
python -m orchd.orch_reload attach-stop-preflight --state /srv/example/state.db \
  --operation 00000000-0000-4000-8000-000000000001 \
  --preflight-file /srv/example/pre-stop.json --preflight-sha256 EXACT_RAW_SHA256
```

The file must be an owned regular file, not a symlink, not writable by group or
others, saved before the operation, and match the explicit raw SHA. The snapshot
must match the exact original Orch/session/job/model/cwd and positive PID/provider
start descriptor. The Operator attests that this protected pre-stop snapshot was
captured from the same provider profile. Attachment records the provider profile
and digest immutably; conflicting attachments fail. It neither stops nor spawns.
After attachment, repeat the original `reload` command with the same operation
and identity. If any positive evidence is missing, it remains `stop_pending`.
