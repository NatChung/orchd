# Exact channel approval (PR5)

Outbound remains disabled in `channel/channels.toml` (`outbound_enabled = false`).
There are no standing grants or automatic replies. Enabling a real sender and
providing platform accounts require a separate Operator decision. The proposed
defaults still await NatChung's confirmation.

The local flow is:

```text
draft -> exact Entry question/answer -> approve -> transaction/CAS claim
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
message ID). A free-form "can handle it", a question/reference from another
owner, a changed question, or an untrusted reply does not authorize sending.
Entry's existing same-OS-user trust limitation still applies; actor strings
and Python objects are not an OS security sandbox. Questions must fit Entry's
existing size limit; no truncated preview is approved.

Only a provisioned adapter can claim and record outcomes. The common
`OutboundAdapter` resolves source-scoped secret references and uses a separately
injected send transport factory. Existing receive adapters' `send()` methods
accept that factory, never a raw message. Normal runners do not enable an
outbound transport implicitly. The cooperative runner can explicitly receive
an outbound sweeper. Gmail additionally needs its source's `send_enabled = true`
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
