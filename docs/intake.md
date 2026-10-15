# Intake contract v2 (additive polling contract)

Source: the polling design in NatChung/nat-assistant#8 (work package W1).
Baseline: intake v1 at 890527a. Event payload `version=1` and snapshot schema v2 remain unchanged;
contract v2 adds exclusion and bounded keyset pagination. Existing commands and JSON keys remain supported.
Fixtures: `tests/fixtures/intake/`. Code: `orchd/intake.py`. Polling is not enabled by this change; L1 auto-send is disabled.

Producers never touch SQLite. They call `orchd intake ...`; every call prints exactly one JSON receipt and exits
0, or 1 when `status` is `failed`. A rejected call writes nothing.

## Producer grant (config, read-only)

`~/.config/orchd/config.toml` (or `ORCHD_CONFIG_FILE`); edit by hand, orchd has no writer for it:

```toml
[intake.producers.email-adapter]
providers = ["email"]
accounts  = ["alice@example.com"]
```

No grant, or an unset allowlist, rejects everything (`producer_not_allowed`, message names the producer).
A malformed section fails closed. Identity comes only from `--producer` plus this grant, never from the payload.

## Commands

| argv | input | result `status` |
|---|---|---|
| `orchd intake submit --producer P [--file F\|-]` | event JSON | `received` / `duplicate` |
| `orchd intake pending [--owner ORCH_ID] [--limit N] [--cursor C]` | – | (list; omit `--owner` = unassigned; consumes nothing) |
| `orchd intake bind --receipt R --owner ORCH_ID` | – | `bound` / `unchanged` |
| `orchd intake ack R [R...] [--actor orch\|operator]` | – | `acked` (each receipt `acked`/`duplicate`) |
| `orchd intake exclude --receipt R [R...] --reason CODE --rule-version V --actor orch\|operator` | – | `excluded` (each receipt `excluded`/`duplicate`) |
| `orchd intake classify --receipt R [--file F\|-]` | `{classification A-D, level L0-L2, todo_id, issue_ref, project}` | `classified` |
| `orchd intake draft [--file F\|-] [--actor]` | draft JSON | `drafted` / `unchanged` |
| `orchd intake draft-show --draft D --version N` | – | `draft` (read-only; full draft incl. `body`, `body_sha256`, `state`, `is_latest`, `expired`, `approval_id`) |
| `orchd intake approve [--file F\|-] [--actor orch\|operator]` | approval JSON | `approved` / `duplicate` |
| `orchd intake claim --draft D --version N` | – | `claimed` |
| `orchd intake receipt [--file F\|-] [--actor connector\|operator]` | receipt JSON | `recorded` / `duplicate` |
| `orchd intake cancel --draft D [--actor]` | – | `cancelled` / `duplicate` |

## Event (submit)

Required: `version` (=1), `provider` (`email|slack|line`), `account`, `event_id`, `source_ref`, `source_time`
(finite unix seconds; inf/nan rejected), `summary` (≤1000 chars). Optional: `company` (default = account), `thread_ref`, `owner_orch`.
Any other field (including `producer`, `sender`, `body`) is rejected `unknown_field`. No full text is accepted.

Unique key: `(provider, account, event_id)`. Same key + same content → original receipt, `status=duplicate`.
Same key + different content → `conflict`, nothing overwritten. `owner_orch` must be a registered Orch
(`unknown_owner`); omitted = `owner_state: unassigned`, never auto-assigned.

Receipt: `status, receipt_id, provider, account, event_id, received_at, state (received|acked|excluded), owner_orch,
owner_state (assigned|unassigned), content_sha256`. The scanner advances its checkpoint only after this receipt.
`ack` is a separate later stage and requires an owner. Additive receipt fields:
`excluded_at, excluded_by, exclusion_reason, exclusion_rule_version` (null until excluded).

## Lifecycle and explicit exclusion

Lifecycle: `received → classified → bind → ack / excluded`. `classified` and `bind` are operations,
not new persisted states: classification annotates the event and binding assigns an owner while state
remains `received`. Unassigned events may be excluded directly; no owner is required.

The producer calls `submit` to obtain a durable receipt. Orch calls `classify` and `bind` after triage.
Orch or Operator calls `ack` **only when the owner has durably taken responsibility for handling the event**
(「owner 已持久接手處理」). Generating a digest, selecting a Top 5, successful submit, bind alone,
and worker done **do not count as ack conditions**. Ack is not business completion.

Only an explicit `exclude` CLI call by Orch or Operator records an exclusion decision. `--reason` is a
nonempty reason code, `--rule-version` a nonempty policy version, and `--actor orch|operator` is required
(no default); reason and version are at most 200 characters. Never derive exclusion authority from
third-party summary text or event payload fields. Excluded means removed from intake processing,
**not** business done or acked. It retains its source reference, event and deduplication key: resubmitting
the same event is still `duplicate`. No source deletion or purge occurs.

Repeat the same decision (receipt, reason, rule version, actor) → per-receipt `duplicate`, no log/revision
change. A changed decision on an excluded receipt → `conflict`. A batch uses one transaction: missing,
acked or conflicting receipts reject the whole batch without changes. Repeated receipt IDs are collapsed.
Acked events cannot be excluded. Classification requires state `received`; acked and excluded events
reject classification with `invalid_state`. Excluded events cannot be bound (even to the same owner), acked
or used for a draft. Each newly excluded receipt appends one `exclude` log row and increments snapshot revision.

## Pending traversal

The default remains the first 100 `received` events ordered by ascending id, unassigned unless `--owner`
is specified. Reads never consume or acknowledge events. All previous `owner`, `events` and event JSON
fields remain; additions are event `id` and exclusion fields, and response `next_cursor`, `has_more`,
`high_water_id`. `--limit` is a positive integer.

Start `orchd intake pending [--owner X] --limit 100`. While `has_more` is true, pass `next_cursor` to
`--cursor` using the **same owner queue**. The opaque cursor carries the exclusive last id, initial
high-water id and owner scope. The final page has `has_more=false`, `next_cursor=null`. Start the next
polling traversal without a cursor; do not repeatedly read only the first page. This visits old unresolved
items and later received items without letting newly submitted events extend a traversal forever.

Manual equivalent: `--after-id N --high-water-id H`; both are nonnegative integers, N ≤ H. Without H,
a call captures the current max event id. Carry H on every subsequent page to preserve the boundary;
`--after-id` alone does not preserve a prior boundary. Cursor cannot be mixed with either manual flag.

The high-water bounds IDs, **not a frozen membership snapshot**. Each page evaluates current queue and
`received` state. Acked/excluded events and events bound out before their page are skipped. Events bound
into a queue ahead of its cursor can appear once in the current traversal; those bound in behind the cursor
are visited on the next traversal. New submissions above H also wait for the next traversal. IDs never
repeat within one traversal. Consumers must finish each traversal then restart at the beginning, including
when the last page is empty after concurrent changes. This guarantees eventual visitation of items that
remain pending, without claiming business completion or a frozen queue across concurrent mutations.

## Draft → approval → send

States: `pending_approval → approved → sending → sent | failed | uncertain`; also `cancelled`, `superseded`.
Draft input: `{draft_id?, receipt_id?, provider, sender, target, source_ref, level (L0-L2, default L2), template?,
summary, body, expires_at}`. If `receipt_id` is supplied, the event must exist and must not be excluded.
An excluded receipt rejects new, revised and identical draft submissions with `invalid_state`, without
changing existing drafts, approvals or logs. Received and acked receipts remain eligible for drafts;
drafts without a receipt remain supported.
Resubmitting identical content returns `unchanged`; a change to any persisted field (receipt_id, provider, level, template, summary, body, sender, target, source_ref, expires_at) is a new `version`; older versions become `superseded` and their
approval stops counting. Approval input must be exactly
`{draft_id, version, body_sha256, sender, target, source_ref, idempotency_key}` (`body_sha256` = sha256 of the body
as shown in the draft receipt). Errors: `stale` (not latest version / hash differs), `target_mismatch`, `expired`,
`invalid_state`, `idempotency_conflict` (same key, different content). Same key + same content replays the original.

`claim` (approved → sending) hands the connector `body` and `send_idempotency_key` once; a second claim is
`already_claimed`, so nothing is automatically resent. `receipt` input
`{draft_id, version, outcome: sent|failed|uncertain, connector_receipt_id (required for sent), detail?}`;
`sent` exists only through it. `uncertain` blocks further automatic action; only `--actor operator` may resolve it
to `sent`/`failed`. A kanban approve button is not a connector call; it approves an exact version only.

## Error codes

`invalid, unknown_field, producer_not_allowed, account_not_allowed, unknown_owner, conflict, not_found, stale,
expired, target_mismatch, invalid_state, already_claimed, idempotency_conflict`. Failure JSON:
`{"status":"failed","error_code":...,"error":...}`.

## Snapshot (`orchd snapshot --json --schema-version 2`)

Additive key `intake` (absent from v1, which is unchanged). Read-only on a private copy; consumes and migrates nothing.

```
intake: {version: 1, available: bool, revision: int|null,
  auto_send: {enabled: false, reason},
  warnings: [{code: unassigned|owner_stopped|retention_expiring|retention_expired, receipt_id, provider, account}],
  sources: [{provider, account, company, received, unacked, excluded, last_received_at}],
  items:  [{receipt_id, version, provider, account, company, event_id, source_ref, thread_ref, source_time,
            received_at, producer, summary, owner_orch, owner_state (assigned|unassigned|owner_stopped),
            state, acked_at, acked_by, excluded_at, excluded_by, exclusion_reason, exclusion_rule_version, id, classification, level, todo_id, issue_ref, project,
            retention (ok|expiring|expired|acked|excluded), trust: "third_party_data", draft_refs: [{draft_id, version}]}],
  drafts: [{draft_id, version, receipt_id, provider, sender, target, source_ref, level, template, summary,
            body_sha256, expires_at, state, created_by, created_at, updated_at, connector_receipt_id,
            outcome_detail, is_latest, expired, approval_id}]}
```

`available: false` (DB without the intake tables) means "not connected", never zero items. `revision` is the max
`intake_log.seq`; it changes on every state change in the same DB file, so the existing DB/WAL watch refreshes SSE.
Source `received` still counts all durable events; `unacked` still counts only `state=received`.
New `excluded` counts terminal exclusions separately. Excluded items project `state=excluded`, exclusion
metadata and `retention=excluded`; they produce no pending-owner or retention warnings and do not gain ack
fields. Dashboard consumers of schema v2 must render excluded separately from pending and acked/done,
display the reason/version/actor, and handle the additive source count and retention value. Dashboard
implementation is a separate work package.

Bodies of events and drafts are never projected in the snapshot; W5 reads a draft body before approval with `draft-show` for the exact version. Items are the newest 500.

## Additive migration and backup validation

`store.connect` adds only four nullable columns to existing `intake_events`: `excluded_at REAL`,
`excluded_by TEXT`, `exclusion_reason TEXT`, `exclusion_rule_version TEXT`. It rewrites no existing rows,
states, keys, logs, tasks or drafts. Migration is repeatable. Snapshot remains read-only and supports
unmigrated v1 intake tables by projecting new exclusion fields as null. No event payload or snapshot
schema version changes are required.

Before rollout, use SQLite's backup API to copy a synthetic populated v1 database (including received,
classified, acked events, drafts, approvals and logs), open **only the backup** with `store.connect`, and
compare every original table/column value before and after. The automated regression
`MigrationV2Tests.test_populated_v1_backup_additive_preserves_all_old_rows_and_original` performs this check,
checks the four additions and repeat migration, verifies exclusion works on the migrated copy, and asserts
the original database bytes are unchanged. Production DB migration is outside this package.

## Contract v3: intake to Orch (event `version=2`, notification, decisions)

Issue NatChung/orchd#47; design: nat-assistant `docs/design/intake-to-orch.md`. Intake collects and suggests;
the owning Orch decides. Naming: the header above calls the earlier command set "contract v2"; the **payload**
`version` stays `1` for those events. This section is payload `version=2`. Version-1 payloads and every existing
command keep working unchanged. Nothing here changes runtime, launchd or the installed W7 flow; scheduling
`orchd intake deliver` and switching W7 are separate, approved rollout steps.

### Event version 2

`orchd intake submit` accepts the v1 fields plus `content` and an optional `suggestion`:

```json
{"version": 2, "provider": "email", "account": "…", "event_id": "…", "source_ref": "…", "source_time": 1760000100,
 "summary": "≤1000 chars", "owner_orch": "optional",
 "content": {"status": "complete|truncated|unavailable", "text": "original, ≤100000 chars",
             "unavailable_reason": "only with unavailable", "attachments": [
               {"name": "…", "sha256": "64 hex", "bytes": 2048, "status": "referenced|unavailable", "note": "…"}]},
 "suggestion": {"routing": {"project", "repo", "mode", "rule_version", "evidence"},
                "triage": {"classification", "urgency": "urgent|normal", "reason"},
                "related_issues": [{"issue_ref", "state": "open|closed|unknown", "reason"}],
                "candidates_status": "complete|candidates_unknown|none",
                "speaker": {"person_id", "name", "company", "role", "confirmation": "confirmed|unconfirmed|unknown",
                            "roster_version", "forwarded_by"},
                "conversation": {"platform", "kind": "group|channel|dm|email", "ref", "name"}}}
```

* The original text is stored in `intake_content` (private, local, never in snapshot or GitHub). Attachments are
  descriptors (digest, size, availability), not bytes; `unavailable` content carries no text and a reason, so a
  summary can never pass for the original. Limits: 100000 chars of text, 20 attachments, 20000 bytes of suggestion,
  20 related issues; unknown fields are `unknown_field`.
* Identity hash = v1 identity fields + text digest + attachment descriptors. Suggestions are **not** in it:
  replaying the same event with a different suggestion returns `duplicate` and appends suggestion `revision` N+1;
  different text or attachments is `conflict`. `orchd intake suggest --producer P --receipt R` appends a revision
  to a received v2 event without resending it (same grant check; identical suggestion is `unchanged`). Producers
  can only suggest; they cannot write decisions.
* `orchd intake show --receipt R --as ORCH|operator` is read-only and returns text, attachments, latest suggestion,
  its history and the per-receipt `revision`, marked `trust: third_party_data`. Only the owner or `operator` may read it.

### Notification outbox

One `intake_notifications` row per (receipt, owner), written in the same transaction as the submit or bind, so a
crash cannot lose it (replay adds nothing; a rebind withdraws the old owner's row and queues the new owner).
`orchd intake deliver` (also the recovery scan: it first creates missing rows for every received event that has an
owner, including v1 events and every owner queue) batches pending notifications per owner:

* urgent (suggested `triage.urgency=urgent`) is due immediately and `submit`/`suggest`/`bind` try it right after commit;
  normal is due after a batch window, default 30 minutes. An upgrade to urgent makes a pending row due now; a row
  already delivered is never re-sent, and urgency never goes down.
* Before transport, the rows are durably marked `uncertain` and their attempt counters incremented. Wake text
  carries delivery identities `notification_id:attempt`. A crash or exception leaves an uncertain outcome,
  visible in `intake backlog` with the notification id, attempt and error. `deliver` never automatically retries
  uncertain rows. A retired Claude Orch is revived like the entry does. A wake never acks a receipt.
* After checking transport history, use `orchd intake notify-reconcile ID ATTEMPT --delivered yes|no --detail TEXT`.
  `yes` confirms delivery; `no` authorizes retry by changing the row to `failed`. The id and attempt are a CAS;
  stale reconciliation is rejected. A mistaken negative read-back can cause a duplicate wake. This protocol
  records uncertainty; it does not claim exactly-once transport or receiver deduplication.
* Wake text starts `[orchd intake] third-party data:` and names counts and receipt ids only, never the text; it never
  imitates an Operator entry message. It ends with a reminder of the handling rule (also in the Orch home managed block, `orch-rules-v2`): an intake message is automatic, so the Orch decides the project, opens or merges into an issue with a plain-language summary and source, classifies it (to do / record only / needs Operator decision), does not reply to the source and does not ask the Operator per message; decisions are batched as a decision card on the issue. Outward replies still need Operator approval unless a standing authorization exists. Unassigned receipts have no one to wake; they show in `intake backlog`.

### Decisions

Orch tools (`intake_pending`, `intake_show`, `intake_decide`, `intake_resolve`, `intake_decisions`, `intake_backlog`
over MCP; `orchd intake decide|resolve|decisions|backlog` on the CLI). Over MCP the caller's own thread is the only
identity: there is no actor or owner argument. `decide` payload: `receipt_id`, `revision`, `action`, `reason`,
`idempotency_key`, and `issue_ref` **or** `issue_na_reason`, `goal_id` **or** `goal_na_reason` (never both, never
neither; null refs are allowed only with a stated reason), optional `approval` (a recorded reference, not proof).

| action | what it requires | completes |
|---|---|---|
| `attach_existing` | `issue_ref` | at `decide`: sets the issue_ref, acks |
| `record` | reasons | at `decide`: acks |
| `ignore` | reasons | at `decide`: excludes (`exclusion_reason` = decision id) |
| `create_issue` | `issue_ref` empty | at `resolve`: `issue_ref` + `external_receipt` (the read-back) |
| `ask_nat` | refs or reasons | at `resolve`: matching `question_id` with this decision’s `question_context` |
| `dispatch` | github `issue_ref` and `goal_id` of the same repo | at `resolve`: your `task_id`, on that goal, linked to that issue |

`create_issue`, `ask_nat` and `dispatch` record an **intent** first. The Orch does the outward step, then `resolve`
checks the evidence, completes the decision and acks the receipt in one transaction. `outcome: uncertain|failed`
records the state without acking (`uncertain` stays open and blocks retention; `failed` closes it for a new decision).
A crash after the outward step leaves the open intent; repeating the same action returns it (`status: pending`)
instead of creating a second ticket, question or worker, and `resolve` completes it after the Orch reads back what
exists. This is recoverable and reconcilable, not exactly-once across systems.

For `ask_nat`, copy the returned `question_context` unchanged into the question body and use the decided
`issue_ref` when calling `ask_nat`. Resolution checks that the question is your Orch’s outgoing question, was
created after the intent, has the same issue ref, and contains the exact receipt/decision/issue/goal context.
The decision saves the question id and keeps those associations in its trace. A different receipt’s question,
even for the same issue, cannot complete this decision.

If ownership changes or a new suggestion invalidates an open intent, first read back the external system.
The **current receipt owner** can call `resolve` with `outcome: failed` and a non-empty `detail` describing that
reconciliation, even if another Orch created the intent. This never acks; it preserves the original decision
and records the reconciling owner, detail and optional external evidence. Then decide again using the current
revision, adopting any external issue/task already found rather than repeating the outward action. Old owners
are refused. Completion checks every intervening receipt mutation (including A→B→A); ledger-only uncertain
progress does not invalidate a claim.

Guards: compare-and-set on the receipt's revision (newest `intake_log` row for it, so a new suggestion, bind or
decision makes older reads `stale`) and on the owner (`not_owner` after a rebind or on an unassigned receipt);
one unresolved decision per receipt (`already_claimed`); `idempotency_key` replay returns the original decision,
a different payload is `idempotency_conflict`. All history is kept in `intake_decisions`; `intake decisions`
shows receipt -> decision -> issue / goal / task / question with actor and reason.

`ack` of a **version-2** event requires a completed decision (it is acked by `decide`/`resolve`); version-1 events
keep today's `bind` -> `ack`, so installing this version does not break a running W7. `exclude` is unchanged.
An unresolved decision shows as `retention: held` in the snapshot (retention purge itself is still not implemented).

### Additive migration

`store.connect` adds the bridge tables and the nullable `intake_content.unavailable_reason` column. Source
unavailability reasons are retained and returned by `intake show`. Existing rows and keys are preserved.
Schema creation, column additions and indexes run in one `BEGIN IMMEDIATE` transaction; any failure rolls back
the whole upgrade and closes the connection. Isolated upgrade tests cover success, injected schema failure,
and reopening after rollback. Older code can ignore the additive tables/column after a successful upgrade.

## Outside this package

Source adapters (W2/W3), MAP/TODO classification logic (W4), dashboard UI (W5), runner and launchd (W6; including scheduling `orchd intake deliver`), pilot (W7).
Retention purge (7-day/90-day) is not implemented; the snapshot only warns. Pre-send re-checks (did Nat already
reply, did the source change) belong to the connector before `claim`.
