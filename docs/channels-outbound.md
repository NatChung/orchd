# Exact channel approval (PR5)

Outbound remains disabled in `channel/channels.toml` (`outbound_enabled = false`).
Standing grants can waive per-message Entry approval for Orch-authored drafts.
Enabling a real sender and providing platform accounts remain separate Operator
decisions. Creating a grant does not enable outbound or create a draft.

The local flow is:

```text
draft -> exact Entry question/answer OR active standing grant -> approve -> transaction/CAS claim
      -> adapter platform call outside transaction -> receipt or uncertain
```

`channels draft --file - --json` takes `message_id` and `payload`. The payload
requires `sender`, `target`, `body`, `expires_at` (UTC Unix timestamp). Optional
fields are `draft_id` (next version), `source_id`, `account`, `thread`, `subject`,
`format` (`text`/`markdown`), `mentions`, `attachments`, and `reply_kind`
(`interim`/`final`/`no_reply`). Source/account/sender must match the provisioned
source; the route must name that same source as its unique responder before
approval/claim. Cross-source reply routing is deliberately refused.

Gmail targets contain `to`, `cc`, `bcc` lists. Each attachment contains `name`,
`sha256`, `bytes` (length), `data_base64` (the actual immutable bytes). The
canonical payload digest binds all those values, source/account/sender, body,
format, mentions, thread, reply kind and the route version/epoch. A changed
payload needs a new version and approval. Legacy body-only approvals remain
valid for legacy flows, but never authorize channel sends.

`channels approval-preview DRAFT VERSION --json` returns the exact question text
and exact affirmative answer. The owning Orch presents that question through
the existing Desktop Entry; Operator replies through its actual relay or
dashboard answer API. `channels approve --file - --json` requires precisely
`draft_id`, `version`, `payload_hash`, `entry_answer_ref` (the stored reply's
message ID). With a matching standing grant, omit `entry_answer_ref` (or use
JSON null); approval then requires no Entry answer. A free-form "can handle it", a question/reference from another
owner, a changed question, or an untrusted reply does not authorize sending.
Entry's existing same-OS-user trust limitation still applies; actor strings
and Python objects are not an OS security sandbox. Questions must fit Entry's
existing size limit; no truncated preview is approved.

Only a provisioned adapter can claim and record outcomes. The common
`OutboundAdapter` resolves source-scoped secret references and uses a separately
injected send transport factory. Existing receive adapters' `send()` methods
accept that factory, never a raw message. Normal runners do not enable an
outbound transport implicitly. For Slack, explicitly set `outbound_enabled = true`
in `[channels.slack."SOURCE_ID"]` and the global `[channels]` setting, then
restart serve. Only routes in `respond` mode with that source as responder can
send. The cooperative runner sweeps approved deliveries for each enabled source. Gmail additionally needs its source's `send_enabled = true`
in `email/config.toml`, a distinct `send` credential reference, and `gmail.send`;
the read client is never reused. Credentials are never in claims or receipts.

Slack supports text/markdown with explicitly approved user mentions (no broad
channel mentions); LINE uses text push, never a reply token; Gmail sends MIME
with exact To/Cc/Bcc, subject, thread and attachment bytes. Slack files and LINE
files/mentions/threads are unsupported and fail closed, rather than silently
omitting approved content or introducing a second external operation. Platform
eligibility, quota and permissions can still reject any send. Production SDK,
OAuth and live platform behavior have not been exercised by the synthetic tests.

Claims atomically mark the draft `sending`, delivery `claimed`, and attempt
`started`. No claim is reclaimed automatically. Timeout, missing receipt and
any ambiguous platform exception become `uncertain`. A DB failure after send
leaves the claim for explicit recovery; sweeping cannot send it again.
`provider_accepted` means only a provider receipt, never recipient read/delivery.
`no_reply` returns a silent decision and creates no outbound delivery; interim
and final drafts have separate approval/delivery identities.

Before `channels recover-outbound --json`, stop/fence the previous sender. It
marks abandoned claims uncertain without retrying. Investigate with provider
records/spool, then use `channels reconcile --file - --json` with `delivery_id`,
`outcome` (`provider_accepted`, `failed`, `uncertain`), `provider_receipt` (only
for acceptance), and nonempty `evidence`. This control-plane operation appends
an investigation record; it preserves the original unknown attempt and never
queues another send. If the provider cannot prove an outcome, retain uncertain.
`channels delivery ID --json` exposes the delivery and attempt history.

Rollback: stop new claims/senders first, retain the additive draft/approval
columns and delivery/attempt ledger, and reconcile in-flight/unknown effects.
Do not restore an old DB or drop the ledger to retry: external sends cannot be
undone by rolling back code or storage.


## Standing grants

Only Operator's local terminal can create or revoke a grant. The CLI uses the
existing channel identity resolution: `ORCHD_WORKTREE` resolves a worker;
`ORCHD_ORCH_ID` or `CODEX_THREAD_ID` resolves an Orch with configured source ACLs;
only a terminal outside those agent contexts resolves Operator. Workers, Orch
sessions and third-party inputs cannot create grants. Do not unset agent
identity variables to bypass this boundary. This retains the existing same-OS-user
trust limitation; it is not an OS security sandbox. No MCP/Entry grant creation
endpoint is provided, and a provenance string is a record of the original
instruction, not a substitute for Operator identity.

Operator can create the issue #220 scope (all routes on `slack-main`, no expiry)
from a UTF-8 JSON file, for example `grant.json`:

```json
{
  "source_id": "slack-main",
  "reason": "Allow Orch to reply on all routes in this source",
  "provenance": "Entry 629, 2026-11-12: paste Operator's original instruction here"
}
```

Replace the provenance with the actual original instruction and its reference,
then run these commands in Operator's terminal:

```text
orchd channels grant create --file grant.json --json
orchd channels grant list --source slack-main --json
orchd channels grant audit --source slack-main --json
orchd channels grant revoke cg_ID --reason "Stop automatic approval" --json
```

`create --file -` also reads JSON on stdin. Optional `route_id` restricts a grant
to that source's route, `target` restricts it to the exact canonical target
(string for Slack/LINE; recipient object for Gmail), and `expires_at` is a future
UTC Unix timestamp. Omit these fields or use null for unrestricted scope/no
expiry. The returned `grant_id` identifies the persisted grant. Reserved nullable
`max_messages` and `max_per_hour` columns provide space for future limits; this
version exposes no limit settings and imposes no quantity/rate limit.

A grant is evaluated only at approve and rechecked at claim. An Orch can call
`channels approve --file - --json` with `draft_id`, `version`, `payload_hash`
and no `entry_answer_ref`, or MCP `channels_approve` with those three fields.
There is no automatic draft creation or approval on inbound receipt. Draft
provenance records the trusted caller's role, identity and exact payload hash;
auto-approval requires role `orch` and identity equal to the message's owning
Orch. Worker/Operator drafts and older drafts without this provenance require
exact Entry approval. Third-party content cannot supply the caller provenance.
Unmatched, revoked or expired grants require the existing exact approval flow.

Grant approval evidence preserves grant ID, draft/version, payload hash,
sender, target and approval time in an additive ledger. Audit returns this
historical evidence together with delivery ID, current delivery state/update
time, target snapshot and attempt outcomes. `current_approval_actor` distinguishes
a later exact Entry approval from the original grant approval. Source and message
owner ACLs apply to audit reads. `channels delivery ID --json` provides the full
attempt history. List retains revoked grants, creator/time, reason, original
provenance, and revocation actor/time/reason. Expiry is evaluated from `expires_at`
even though the stored lifecycle state stays `active` until revocation.

Revocation immediately prevents subsequent claims using that grant, including
already queued drafts; claim cancels such deliveries without sending. The same
draft can receive exact Entry approval and return to the queue. Already claimed
platform calls cannot be recalled. A revoked grant cannot be replaced implicitly
by another grant at claim. Outbound enablement, route mode/version/epoch, sender
binding and immutable payload checks remain in force. `uncertain` and abandoned
claims are never automatically retried, with or without a grant. Migration 3
adds tables only; retain them and the approval/delivery ledger on rollback.
