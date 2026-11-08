# Slack and LINE receive-only ingress

PR4 adds `SlackAdapter` / `SlackTransport`, `LineReceiver`, a private source-bound
`Spool`, and a persistent loop guard in the unified receive transaction. No new
CLI or MCP tools are registered. Outbound approval, claim and adapter sends stay
hard-disabled, including `no_reply` (zero platform calls).

## Deployment boundary

The proposed defaults are pending NatChung's confirmation: single host, LINE
**direct receiver**, outbound disabled, and PR2 retention (unsend immediately).
The optional cloud edge receiver is **DEFERRED**. An offline direct host does
not receive webhooks; platform redelivery cannot guarantee recovery. Only events
already durably accepted into the local spool can be replayed. This PR does not
provision accounts, listeners, TLS certificates or a public endpoint.

Provision sources and routes through the existing operator control plane. Resolve
credentials only through the adapter's source-scoped OS secret provider. Slack
uses separate `app` and `api` secret references; LINE uses `signing`. There is no
environment-variable or file-secret fallback. `channels-slack` is optional;
`channels-line` needs only the standard library. The existing lock file is not
regenerated.

## Slack lifecycle

Create a dedicated `ChannelService` and `SlackAdapter` on the runner thread.
Configure explicit conversation IDs, an activation timestamp, and known self
user/app/bot identities from trusted configuration. Choose the intended user or
bot Web API account; there is no fallback to another account. Configure the app's
Socket Mode `connections:write`, event subscriptions and conversation-type read
scopes in Slack, separately from orchd. Missing scopes fail reconciliation.

Call `connect()` explicitly, then run the existing `AdapterRunner` with that
adapter. Call `close()` on shutdown. SDK callbacks create their own service
connection and commit a durable receipt before acknowledging `envelope_id`.
Failure leaves the envelope unacknowledged. The SDK owns reconnect; connected
status does not imply recovered history. Poll and requested sync share the same
source lease. History and each discovered thread use separate checkpoints,
bounded pages and a 300-second overlap. A failed page, repeated cursor, missing
scope or unavailable history leaves the affected window uncommitted. Existing
runner backoff/jitter honors transport `Retry-After` on rate limits.

Slack timestamp IDs stay strings. History and Socket Mode share root/thread and
edit identities. Deletes erase every revision's body through PR2 tombstones.
Unknown event subtypes retain only a durable unavailable diagnostic, never a
processing claim. No external body or Slack metadata can assert verified
provenance or Operator authority.

Official contracts consulted: [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/)
and [Python SDK](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/).
Acknowledgements follow local durability; missed acknowledgement deadlines rely
on idempotent redelivery rather than a promise of exactly-once delivery.

## LINE lifecycle and recovery

Construct `LineReceiver` with the provisioned source, adapter caller, secret
provider and a private `Spool(path, source_id)`. Serve `receiver.handler()` on a
dedicated, single-threaded HTTP server on the **same thread** that owns these
objects, behind deployment-managed HTTPS. Use a finite server request timeout
and call `receiver.drain()` between requests / on scheduled ticks, including when
no webhooks arrive. The handler accepts only POST `/webhook`, exposes no operator
or admin routes, bounds Content-Length to 1 MiB and suppresses HTTP request logs.
No public receiver is started by imports or installation.

HMAC-SHA256 checks the exact raw bytes before parsing. Missing/wrong signatures
return 403 and write nothing; missing secret configuration returns 503. Empty
batches succeed. All normalized events in a valid batch enter one spool
transaction before success. The webhook's redelivery flag is excluded from the
stable identity. Unsend is applied to the unified service before success and
scrubs queued bodies, even when it precedes the original message. A failed
privacy operation cannot be treated as completed admission.

Drain pages use local monotonic sequence numbers, not platform event time. The
source lease serializes drains. Ack follows the downstream durable receipt;
a lost ack replays the same event and cannot create another processing claim.
Acknowledged payloads are erased while event hashes remain for deduplication.
SQLite secure deletion scrubs removed spool text; a protected Windows DACL or
POSIX 0600 protects the file. The spool is transmission buffering, not another
processing or approval database. Capacity exhaustion returns 503 and sets
`retention_gap`; stop admission and investigate capacity without skipping or
purging unacknowledged rows. No history endpoint can reconstruct missing LINE
events.

## Loop and privacy policy

Canonical origins deduplicate across accounts/apps. The repository stores the
processing claim, provenance and loop-budget change in the same transaction.
Known self identity and accepted outbound provider receipts suppress sent echoes.
Bot/app markings, including allowlisted apps posting with a human user field,
are unverified automation and remain third-party data. They never reset a budget,
change provenance or promote an earlier suppressed observation into a claim.

Verified internal automation is bounded to four turns per conversation and
correlation and a hop cap of eight. The persisted five-minute window is a rate
ceiling; the lifetime turn cap and exhausted pause remain conservative after the
window and after restart. Human-looking messages do not reset them. There is no
inbound reset API. Peer targeting is disabled in this single-host stage. Future
control-plane reset/peer support must preserve these constraints.

PR2 body/attachment/backup retention remains authoritative. Source ACLs govern
local reads; platform credentials and receipt signatures are never exposed in
queries or logs. All included fixtures are synthetic. Real Slack/LINE accounts,
SDK reconnection against Slack, HTTPS deployment, production OS secret providers
and live sends have not been exercised.

## Rollback

Stop webhook/socket admission and the adapter runner first. Preserve the unified
DB, origin/observation/claim/loop ledger and unacknowledged spool. Revert the code
only with readers stopped and a format compatibility check; do not drop additive
tables or restore an old DB over newly received messages. Do not enable a second
receiver while the new one still runs. Unsend bodies stay unavailable on replay.
