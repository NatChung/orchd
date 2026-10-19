# Shared-Orch interface CLI

External adapters use one existing registered Orch. This is separate from the
Desktop Entry (whose home directory is also named `interface`). No new Orch,
role, binding, network endpoint, or intake integration is created. Until trusted
local registration, there are no interface requests or wakes.

## JSON CLI reference

Run `orchd interface VERB` with one JSON object on stdin. Stdout contains one JSON
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
| `fetch` | `{"name":"slack","token":"secret","conversation":"channel/thread"}` | `[{"request_id":1,"conversation":"channel/thread","reply":"answer"}]` |
| `ack` | `{"name":"slack","token":"secret","conversation":"channel/thread","request_id":1}` | `{"acked":true,"request_id":1}` |
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
access remains trusted. Token rotation and standalone owner rebinding are outside v1.

`register` optionally takes `idle_seconds` (integer 1..604800, default 3600).
Registration renews admission. New accepted submissions and polls refresh an
active lease. Polling an expired/deregistered interface does not reactivate it.
Expiry/deregistration block new requests and never delete accepted work or replies.
Fetch/ack remain available; identical retries of accepted submissions still return
the original ID. A reused `(interface_id, conversation, request_key)` with changed
text conflicts. Admission and dedupe run in one SQLite immediate transaction.

Fetch does not consume replies. A reply has the stable identity `request_id`.
Adapters should record delivery by this ID and ack after delivery; repeated fetch,
ack, and lost responses preserve the same state across database restart. Exactly
once delivery to an external service is the consumer's responsibility.

## Normal Orch tools and paging

- `interface_requests({"after_id":0,"limit":100})` returns an array of unanswered
  requests belonging to the calling Orch: `[{"id":1,"interface_id":"opaque-id",
  "conversation":"channel/thread","body":"question","created_at":123.0,
  "trust":"external_untrusted"}]`. Both arguments are optional.
- `interface_reply({"request_id":1,"text":"answer"})` persists one reply and
  returns `{"request_id":1,"interface_id":"opaque-id",
  "conversation":"channel/thread"}`. Only these two input fields are accepted.
  Destination comes entirely from stored origin. Same-text retries succeed;
  changed-text retries conflict, including after ack. This path never calls
  Desktop Entry transport. Existing Orch caller identity rules apply.

Both request listing and CLI fetch take optional `after_id` (nonnegative integer,
default 0) and `limit` (integer 1..100, default 100). Results are ordered by ID.
Pass the last item's `id` (requests) or `request_id` (fetch) as `after_id` until an
empty page. Cursors do not consume anything. Start each subsequent polling cycle
at 0: replies may be produced out of request order, so permanently saving a high
fetch cursor would miss an older request answered later. Acked replies and
answered requests disappear from their respective listings without shifting IDs.

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

Persistence adds only `interfaces` and `interface_requests` plus their indexes;
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
