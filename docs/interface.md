# Shared-Orch interface CLI

External adapters use one existing registered Orch. This is separate from the
Desktop Entry (whose home directory is also named `interface`). No new Orch,
role, binding, network endpoint, or intake integration is created. Until trusted
local registration, there are no interface requests or wakes.

## JSON CLI reference

Adapter verbs run as `orchd interface VERB` with one JSON object on stdin.
The local Operator `list` and `rebind` commands below use flags and require no stdin. Stdout contains one JSON
value; success exits 0, errors exit 1 with
`{"error":"ValueError","message":"..."}`. No TTY or prompts are needed.
Text fields must be nonblank strings of at most 65536 characters. Keep tokens
outside the repository; feed them through stdin rather than command arguments.
Registration is a trusted local control-plane operation, not a remote endpoint.

The following shapes show required fields; optional fields are described below.

| Verb | Input | Output |
| --- | --- | --- |
| `register` | `{"name":"slack","orch_id":"existing-orch"}` | `{"interface_id":"opaque-id","name":"slack","orch_id":"existing-orch","token":"secret"}` |
| `submit` | `{"name":"slack","token":"secret","conversation":"channel/thread","request_key":"event-id","text":"question"}` | `{"request_id":1,"duplicate":false,"wake_error":null}` |
| `fetch` | `{"name":"slack","token":"secret","conversation":"channel/thread"}` (optional `"kinds":["interim","final"]`) | `[{"request_id":1,"conversation":"channel/thread","reply":"answer"}]` |
| `ack` | `{"name":"slack","token":"secret","conversation":"channel/thread","request_id":1}` (optional `"kind":"interim"`) | `{"acked":true,"request_id":1}` |
| `deregister` | `{"name":"slack","token":"secret"}` | `{"deregistered":true}` |

The first registration sets the shared owner for all interfaces, including after
expiry/deregistration. `orchd orch restart` moves all registrations and requests
to the replacement Orch, atomically with the Desktop rebind when present.
Existing tokens, leases, disabled state, request IDs and reply destinations remain
unchanged. Unanswered requests are woken again for the new owner; pending replies
remain fetchable and ackable. No schema migration or adapter re-registration is
needed. A second owner is rejected. Re-registering a name requires
its existing token; identity and pending replies cannot be taken over. Tokens are
random adapter credentials; only SHA-256 hashes are stored. Local same-user DB
access remains trusted. Token rotation is outside v1. Standalone owner migration is described below.

`register` optionally takes `idle_seconds` (integer 1..604800, default 3600).
Registration renews admission. New accepted submissions and polls refresh an
active lease. Polling an expired/deregistered interface does not reactivate it.
Expiry/deregistration block new requests and never delete accepted work or replies.
Fetch/ack remain available; identical retries of accepted submissions still return
the original ID. A reused `(interface_id, conversation, request_key)` with changed
text conflicts. Admission and dedupe run in one SQLite immediate transaction.

Fetch does not consume replies. A final reply has the stable identity `request_id`;
an adapter that opts into interims (`kinds`) must use `(request_id, kind)`, since an
interim and the final share a `request_id` and deduping by it alone would drop the final.
Adapters should record delivery by that identity and ack after delivery; repeated fetch,
ack, and lost responses preserve the same state across database restart. Exactly
once delivery to an external service is the consumer's responsibility.

## Operator migration and recovery (#81 phase 2)

From a trusted local Operator shell, use the same `ORCHD_HOME` as the adapter.
These commands are local control-plane operations, never MCP tools. `rebind`
rejects calling Orch/worker environments (`ORCHD_ORCH_ID` or `ORCHD_WORKTREE`).
This is the existing same-user trust boundary, not authentication against a
local user who can edit the database or environment.

```sh
orchd interface list
orchd interface rebind --from OLD_ORCH_ID --to NEW_ORCH_ID --dry-run
# Review the source, target, affected interfaces and unanswered count, then apply:
orchd interface rebind --from OLD_ORCH_ID --to NEW_ORCH_ID
orchd interface list
```

`list` prints a JSON array with `interface_id`, `name`, `orch_id`, `disabled`,
`unanswered`, `pending_final`, `pending_interim`, and `pending_no_reply` per
interface, including disabled and expired registrations. Counts reflect stored,
unacked delivery state; dismissed requests suppress their unsent interims.
It never prints credentials, refreshes leases, or acknowledges replies.

`rebind` requires distinct, explicit registered source and target IDs. All
registrations, including disabled ones, must still belong to the named source.
A missing/stopped target is refused; source health may be unknown or stopped.
Codex and Claude owners work in either direction. No health probe or runtime
liveness claim is made: the target must exist and have no `stopped_at` marker.
Neither Orch is stopped; tasks, intake, and Desktop Entry binding stay untouched.

Dry-run validates the same conditions and returns `status:"dry-run"`, the old
and new IDs, an interface snapshot, and the unanswered count without changing
ownership, leases or wake state. Applying is the explicit Operator confirmation.
It repeats validation under the existing `interface-owner` delivery lock and one
SQLite immediate transaction. A stale source, failed validation or interruption
before commit leaves ownership unchanged. Tokens, interface/request IDs, lease
expiry, disabled state, routing, reply text, dismissals and ack state survive.
Unanswered requests (including interim-only requests) have wake/backoff state
reset for the new owner. Final/dismissed requests do not wake again.

In-flight old-owner wakes finish before the move. A final/interim reply or ack
committed before the transaction is retained. After commit, old-owner replies
and dismissals fail the stored request-owner check; CLI acks carrying the old
`ORCHD_ORCH_ID` fail the interface-owner check. Adapter token acks remain valid,
since the adapter's identity and pending deliveries survive the move. An already
running model turn cannot be recalled; its late writes are refused. Already
fetched adapter output may still be delivered and must retain the consumer's
existing `(request_id, kind)` deduplication.

An apply result reports `status:"done"`, `committed:true`, `interfaces_migrated`
and best-effort `interface_wakes`. A per-request error or top-level `wake_error`
means ownership committed but notification needs recovery; do not repeat the
forward migration. The supervised recovery runner retries durable pending wakes,
including a crash immediately after commit. Inspect `interface list` after an
interrupted CLI to establish the current owner. Notifications remain at least
once: a crash after transport may repeat a wake, never create a new request.

To restore the original owner (which must still be registered and not stopped):

```sh
orchd interface rebind --from NEW_ORCH_ID --to OLD_ORCH_ID --dry-run
orchd interface rebind --from NEW_ORCH_ID --to OLD_ORCH_ID
orchd interface list
```

Rollback preserves replies produced by the temporary owner and wakes only work
still unanswered. Existing `orchd orch restart` behavior remains unchanged:
it stops/replaces a Claude Orch and migrates interfaces with its other state.
Standalone rebind is the tool for an interface-only Codex ↔ Claude trial.

## Normal Orch tools and paging

- `interface_requests({"after_id":0,"limit":100})` returns an array of unanswered
  requests belonging to the calling Orch: `[{"id":1,"interface_id":"opaque-id",
  "conversation":"channel/thread","body":"question","created_at":123.0,
  "trust":"external_untrusted"}]`. Both arguments are optional.
- `interface_reply({"request_id":1,"text":"answer"})` persists the final reply and
  returns `{"request_id":1,"interface_id":"opaque-id",
  "conversation":"channel/thread"}`. Accepted input fields are `request_id`,
  `text` and optional `kind` (`"final"` default, or `"interim"`; see below).
  Destination comes entirely from stored origin. Same-text retries succeed;
  changed-text retries conflict, including after ack. This path never calls
  Desktop Entry transport. Existing Orch caller identity rules apply.

## Interim and final replies

A request has at most one `interim` reply and one terminal outcome (`final` or `no_reply`).
`interface_reply` without `kind` is the final, so existing callers are unchanged.

- `kind:"interim"` is optional and may be sent once, before the final (e.g. to
  acknowledge long work). It is immutable: the same text is an idempotent retry,
  different text conflicts. After a final exists a new interim is refused (an
  identical retry of the already-recorded interim still succeeds).
- `kind:"final"` behaves as before: same text idempotent, different text conflicts.
- `interface_requests` lists a request until its terminal outcome exists. After an interim it
  stays listed and carries an `interim` field with the recorded text (null before).

Adapter side (compatibility): `fetch` without `kinds` returns only finals, in the
original row shape (`request_id`, `conversation`, `reply`, no `kind`), so an
adapter written for single replies never sees an interim and cannot post it as the
answer. An adapter that supports interims passes `"kinds":["interim","final"]`;
rows then carry `kind`, ordered by `request_id` with a request's interim before its
final. `limit` counts requests, so a page can hold up to twice as many rows. Each
reply is acked separately: `ack` takes `kind` (default `final`; its result adds
`kind` only for `interim`). Acking an interim does not ack the final, and acking
`interim` when none exists is an error. Interim delivery state survives restart
like any other reply state; `orchd orch restart` moves pending interims with their
requests, and a request that has only an interim is woken again for the new owner,
which can still send the final.

Both request listing and CLI fetch take optional `after_id` (nonnegative integer,
default 0) and `limit` (integer 1..100, default 100). Results are ordered by ID.
Pass the last item's `id` (requests) or `request_id` (fetch) as `after_id` until an
empty page. Cursors do not consume anything. Start each subsequent polling cycle
at 0: replies may be produced out of request order, so permanently saving a high
fetch cursor would miss an older request answered later. Acked replies and
answered requests disappear from their respective listings without shifting IDs.

## Silent closure (`no_reply`)

`interface_dismiss({"request_id":1,"reason":"no answer needed"})` is an Orch-only
MCP tool. It returns `{"request_id":1,"kind":"no_reply","terminal":true}` and
never invokes external transport. Reason is required nonblank text (up to 65536
characters), stored with `dismissed_at` in the local database for audit only.
A same-reason retry succeeds without changing the timestamp; a different reason
conflicts. Final and dismissal are mutually exclusive, serialized in the same
SQLite transaction: neither can replace the other. All interim/final writes are
refused after dismissal. Previously delivered messages cannot be recalled.

Consumers opt in via CLI fetch `"kinds":["interim","final","no_reply"]` or
`["final","no_reply"]`. A dismissed request yields only:

```json
{"request_id":1,"conversation":"channel/thread","kind":"no_reply","terminal":true}
```

There is no `reply`, `text`, or `reason` field. This event suppresses any unsent
interim and repeats until CLI `ack` with `"kind":"no_reply"`. Ack is idempotent,
scoped to credentials and conversation, and refused before dismissal. The ack
result includes `kind:"no_reply"`. All state survives database reopen and owner
restart. Dismissed requests disappear from `interface_requests`; duplicate submit,
sweeper and owner migration do not wake them. Dismissal serializes with live wakes.
Three nullable columns (`dismissed_at`, `dismiss_reason`, `dismiss_ack_at`) are
migrated additively on database open; existing replies are preserved.

On `no_reply`, consumers must durably cancel holding timers and suppress all
outgoing sends for that request, then ack. Retry ack after a crash. Deduplicate by
`(request_id, kind)`; process terminal events before starting timers or sending
queued interims. A fetched interim already in flight may race with dismissal;
consumer send coordination is required. Unknown kinds must fail closed: send no
message or fallback status and retain for upgrade/operator handling.

Legacy fetch (no `kinds`, or only interim/final) never sees dismissed requests,
including their unsent interims. Its row schema stays unchanged. A legacy consumer
cannot learn dismissal or stop its own automatic holding timer: upgrade consumer
support before using dismissal. End-to-end silence requires that consumer change;
this repository implements only the Orch contract.

## Required recovery runner

Deploy a supervised runner alongside the adapter, using the same installed Python
and `ORCHD_HOME` as the CLI:

```sh
python3 -m orchd.interface
```

The runner sweeps every five seconds, up to 100 due rows per pass. Configure your
service manager to restart it on exit and boot. Alternatively schedule
`python3 -m orchd.interface --once` recurrently (for example every minute). Run
this even when the adapter has no traffic: client retries alone do not recover a
process killed between persistence and wake. This repository does not install a
machine-specific service. No recovery occurs while all runners are down; accepted
work stays durable and resumes when a runner starts.

Submit commits before its immediate wake attempt. The request row is the durable
outbox, including retry attempts/deadlines. A failure or a process crash remains
eligible for sweeping, with exponential backoff of 5, 10, 20, ... up to 300 seconds.
Identical submit retries respect that deadline. `wake_error` is a sanitized
exception class, not rejection of the stored request. Recovery uses `_notify_orch`,
including existing Claude same-session revival and Codex queue behavior. An
explicitly stopped/missing owner is never restarted by the sweeper. Its pending
requests remain until an Operator restarts the owner; there is no automatic
owner rebind or failover. Replies already
produced require no further wake.

Notifications are at least once: a crash after transport but before recording
success may repeat a wake. Per-request cross-process locks serialize live retries;
backoff is persisted before transport. A wake contains a receipt ID and untrusted
label, never the external body. Successful transport means notification accepted,
not proof the model processed a turn. Live host startup/latency is not guaranteed.

Persistence adds only `interfaces` and `interface_requests` plus their indexes.
Interim support adds two nullable columns (`interim`, `interim_ack_at`) and one
partial index to `interface_requests`, applied additively when the database is
opened; existing rows and replies are untouched and need no adapter change.
Desktop Entry and intake tables and behavior are unchanged.

## Accepted shared-Orch risk

External bodies carry `external_untrusted`; credentials authenticate an adapter,
not an external person as Operator or an Operator approval. The shared Orch keeps
its normal `ask_nat`, `send_to_nat`, `dispatch`, and other tools. External text can
induce their use, context mixing, or selection of the wrong request ID. Immutable
routing guarantees the destination for the selected ID; it does not guarantee
model intent or restrict later tool use.

Operator accepts this residual risk. The consumer, kc-assistant, must enforce its
sender allowlist. That implementation and its verification are outside orchd;
this change does not claim to verify them. No restricted role, extra trust guards,
Desktop text matching, or additional home rules are introduced.
