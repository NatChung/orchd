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

## Accept an existing provider resume copy explicitly

Normal reload remains strict: a returned different session stays held. Some
providers explicitly restore the original transcript into a new controller
session. If the Operator accepts that controller lineage, recover the **existing
copy** with `accept-resume-copy`; this entry point never starts or stops a job:

```sh
python -m orchd.orch_reload accept-resume-copy --state /srv/example/orchd.db \
  --operation 00000000-0000-4000-8000-000000000001 \
  --candidate-job RETURNED_JOB --candidate-session RETURNED_SESSION \
  --roster-file /srv/example/provider-roster.json --roster-sha256 EXACT_RAW_SHA256
```

This exception requires the original operation to have positive stop evidence
and be held specifically for its returned session mismatch. The exact candidate
must already be its persisted spawn receipt. The owned, protected provider roster
must prove `launch.mode=resume`, original `launch.sessionId`, `fork=true` and
`restoresTranscript=true`, with the exact returned outer session/job/cwd and a
launch timestamp after this operation began. Its launch flags must retain the
original resolved model, socket, strict MCP configuration and MCP file path.

The MCP file must use the intended deployment's interpreter, entry point and
complete `runtime.mcp_env` profile, including the logical Orch ID. That profile's
state database must be the database containing the operation. A recovery helper
may be deployed separately from the running SDK; execute it under the intended
MCP interpreter and profile. No runtime-wide upgrade is required by this API.

A fresh provider probe must identify the copy alive on the original socket, with
its PID equal to the roster host or positively identified as that host's child.
Competing original/copy processes, unidentified live jobs, missing evidence,
changed controller identity, incorrect configuration or conflicting receipts
refuse acceptance. The same Orch lock and exact original identity/marker CAS
protect the update.

Acceptance changes only the logical Orch's controller job/session/socket and
clears this reload marker. It preserves the operation's original session as
history and stores an explicit lineage receipt with original/accepted sessions,
provider evidence digest and MCP digest. Every author task, owner, generation
and author session is untouched. Repeating the exact acceptance after success
returns its existing receipt; it never dispatches, retries, adopts or resumes an
author. Report this as an accepted provider controller copy, not as preservation
of the controller's original native UUID.

## Attach an actual recovery receipt after an uncertain launch

An uncertain launch with no persisted receipt remains `spawn_pending`. It is
never evidence that no process was accepted. A trusted local Operator may supply
an actual, separately performed recovery launch receipt and native roster to
`attach-recovery-receipt`. This API does not perform or authorize that launch.
There is no automatic retry, cancellation, pending-marker clearance or adoption.
A delayed first launch may still create a competing controller; an empty provider
listing cannot eliminate this risk.

```sh
python -m orchd.orch_reload attach-recovery-receipt --state /srv/example/orchd.db \
  --operation 00000000-0000-4000-8000-000000000001 \
  --receipt-file /srv/example/recovery-receipt.json --receipt-sha256 EXACT_RAW_SHA256 \
  --roster-file /srv/example/provider-roster.json --roster-sha256 EXACT_RAW_SHA256
```

Both files must be owned regular files, not symlinks, with mode `0600`, at most
1 MiB, and match the supplied raw SHA. Save the roster after the launch finishes;
its native dispatch timestamp must fall within the receipt's launch interval.
Receipt times use epoch seconds; native `dispatch.createdAt` uses milliseconds.
The receipt schema is exact (additional fields are rejected):

```json
{
  "version": 1,
  "operation_id": "00000000-0000-4000-8000-000000000001",
  "original": {
    "orch_id": "example-owner",
    "session_id": "original-session",
    "job_id": "original-job",
    "model": "example-model",
    "home": "/srv/example/home",
    "socket": "/srv/example/runtime/orch.sock"
  },
  "launcher": {
    "command": "/opt/provider/actual-version/claude",
    "config": "/srv/example/provider-config"
  },
  "started_at": 1800000000.0,
  "finished_at": 1800000001.0,
  "exit_code": 0,
  "native_stdout": "claude attach returned-job",
  "receipt": {
    "job_id": "returned-job",
    "session_id": "returned-copy-session",
    "socket": "/srv/example/runtime/orch.sock"
  }
}
```

The launcher must produce this file from the actual successful native response,
using exclusive creation; extract only the real `claude attach JOB` line into
`native_stdout`. Do not construct a receipt by selecting a roster candidate.
Do not include full stdout, prompts, tokens, replies, launch argv or credentials.
`launcher.command` is the resolved actual executable path; the executable must
still exist. It is saved separately from the immutable original provider reader
identity. Its config namespace must match the original profile. Continue using
the original reader executable/profile for fresh verification; do not relabel
that preflight evidence to match a newer launcher.

The trusted Operator attests to artifact provenance. Ownership/mode and SHA bind
bytes; they are not cryptographic authentication of who launched the process.
The API also independently requires every original resume-copy lineage,
model/socket/MCP/profile/database, positive live PID/parent, and listening-socket
check described above. It rejects original-session processes, competing copies
with different outer UUIDs but the same resume parent, live jobs missing session or absolute cwd evidence,
and other new active controllers in the same home. Unrelated author jobs are
preserved. Accepted same-parent copies or new same-home workers in the protected
native roster also refuse attachment even before they appear in the public jobs
listing. Every roster worker must have a nonempty absolute cwd; a supplied
dispatch cwd must be absolute and resolve to the same directory. Unknown scope
is refused even if no public job has appeared. Missing, conflicting or ambiguous
evidence leaves state unchanged.

A successful attachment only saves an immutable recovery receipt and changes the
operation to `held/returned_session_mismatch`; it exits 2 and does not change the
controller or its stopped marker. Original preflight/stop evidence and the
previous error remain recorded. Invoke the existing `accept-resume-copy` with
that exact job/session and protected roster to accept the controller, then run
session tool verification separately. This API handles a new native UUID copy;
for a genuine original-UUID receipt, use normal `reconcile` instead. Exact
attachment replay rechecks live evidence; changed evidence is rejected.

Launch failures retain structured `spawn_failure` diagnostics: exception class,
the allowlisted `native_no_job_receipt` stage for missing native attach receipts,
finite numeric errno/return code/timeout when supplied, and captured output byte
counts/digests when supplied. Exception text, command arguments and native output
contents are never persisted. A diagnostic or an exit-zero launch receipt is not
proof of controller readiness.
