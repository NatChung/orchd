# Single-host channel rollout and rollback

These are proposed defaults pending NatChung's confirmation: single host,
manual conversion of unknown legacy formats, no private repository scanning,
and retention as implemented in PR2. Real rollout, source authorization and
enablement require a separate approved operation. PR7 peer ownership is outside
this procedure. All names below are synthetic opaque source IDs.

## Inventory and stop order

1. Record the reviewed artifact, `channels layout plan`, and
   `channels status --json`. The latter is the local source/adapter inventory;
   `compatibility` means cutover fencing has not been activated. Repository
   boundary tests inventory platform imports and tool registrations. Neither
   inventory scans external repositories or proves another OS process stopped.
2. Inventory deployment-managed old polling jobs, direct connector registrations
   and OAuth configuration explicitly. Disable their schedules/tool access,
   stop processes and verify exit before admitting the adapter. Do not copy PID
   locks or credentials. Unrecognized old state needs manual conversion, not an
   automatic connector fallback. Same-UID arbitrary programs require OS secret
   ACLs and egress isolation; the local fence is not an OS security boundary.
3. Stop the current adapter and release its source lease. Freeze using
   `orchd channels cutover --source demo-source --mode frozen --json`.
   Active leases reject this operation. An expired runner must also be confirmed
   stopped: a source fence cannot cancel an external send already in flight.
   Abandoned claims become uncertain and retain their attempt history.

## Shadow and take over

After stopping the legacy receiver, set `--mode shadow`. Feed synthetic or
explicitly approved captured events through the adapter API for comparison;
shadow stores receipts but creates no work claims, wakes, issues or sends.
An old receiver must not run concurrently. Record the reference ledger and
compare stable origin/revision keys and receipt IDs, not raw private bodies.
Freeze again, capture `channels ledger --source demo-source --json`, and note
its fixed `high_water`. `--through N` bounds message history;
`--after N` returns incremental messages. Delivery/attempt/work metadata are
always current so new outcomes are not lost behind a message watermark.

After verifying the inventory and reference comparison, switch the frozen
source to `--mode adapter`. Replay shadow events with the same stable keys:
the original receipt is kept and work is claimed once. Reacquire the singleton
lease before scheduling polling or sync jobs. Old intake/interface platform
entry points are fenced. Disabled templates and outbound flags stay disabled;
cutover does not authorize provisioning, account access or sending.

## Restore a legacy receiver

Stop scheduling and sending, stop the adapter, then freeze. Keep the current
DB, new receipts, sent/unknown attempts, checkpoints and unacknowledged spool.
Investigate every uncertain external effect through the existing reconciliation
API. Unknown outcomes are never automatically requeued. External accepted sends
cannot be undone by restoring files or the database.

Export a fixed high-water ledger, then incremental messages after that point
and current delivery/attempt metadata. Backfill the recognized old receiver's
ledger using original stable keys. Preserve deduplication and terminal outcomes;
do not restore an old DB snapshot over newly received messages. Unknown formats
require an explicit converter in a separate change. The export is metadata-only;
authorized retained content is available through the normal source-scoped API.
After backfill is verified, capture the final ledger while frozen, and pass its
digest to `channels cutover --source demo-source --mode legacy
--backfill-digest DIGEST --json`. This is the Operator's acknowledgement of
external backfill, not a claim that orchd converted an unknown format. New data
or changed outcomes invalidate the digest. Uncertain outcomes block restoration.
Start only the one legacy receiver. The new adapter's receive/lease/send paths
are then fenced. Legacy outbound paths keep the channel approval gate; this
operation does not reopen a direct-sender bypass.

## Files and program rollback

`channels layout rollback --migration ID --dry-run` previews file rollback;
`--apply` removes only unchanged migration-created files and empty directories.
User edits report conflicts; nonempty spool/cache directories remain. Home,
interface, Entry bindings and trust are outside that receipt. Interrupted
provisioning resumes with `upgrade --migrations-only`. Retain additive channel
tables when disabling features. Before restoring a pinned older artifact,
verify that it understands the retained ledger and intake/interface formats.
Layout rollback neither restores a cursor nor retracts a platform message.
