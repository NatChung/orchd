# Intake contract v1 (frozen for W2–W5)

Source: nat-assistant `docs/superpowers/specs/2026-10-13-polling-orch-dashboard-design.md` (W1), Refs NatChung/nat-assistant#8.
Baseline: orchd `main` at f0a5c43 (snapshot schema v2, `entry-question`, `task-question` already present).
Fixtures: `tests/fixtures/intake/`. Code: `orchd/intake.py`. Polling is not enabled by this change; L1 auto-send is disabled.

Producers never touch SQLite. They call `orchd intake ...`; every call prints exactly one JSON receipt and exits
0, or 1 when `status` is `failed`. A rejected call writes nothing.

## Producer grant (config, read-only)

`~/.config/orchd/config.toml` (or `ORCHD_CONFIG_FILE`); edit by hand, orchd has no writer for it:

```toml
[intake.producers.nat-assistant-email]
providers = ["email"]
accounts  = ["nat@ariontechs.com"]
```

No grant, or an unset allowlist, rejects everything (`producer_not_allowed`, message names the producer).
A malformed section fails closed. Identity comes only from `--producer` plus this grant, never from the payload.

## Commands

| argv | input | result `status` |
|---|---|---|
| `orchd intake submit --producer P [--file F\|-]` | event JSON | `received` / `duplicate` |
| `orchd intake pending [--owner ORCH_ID] [--limit N]` | – | (list; omit `--owner` = unassigned; consumes nothing) |
| `orchd intake bind --receipt R --owner ORCH_ID` | – | `bound` / `unchanged` |
| `orchd intake ack R [R...] [--actor orch\|operator]` | – | `acked` (each receipt `acked`/`duplicate`) |
| `orchd intake classify --receipt R [--file F\|-]` | `{classification A-D, level L0-L2, todo_id, issue_ref, project}` | `classified` |
| `orchd intake draft [--file F\|-] [--actor]` | draft JSON | `drafted` / `unchanged` |
| `orchd intake approve [--file F\|-] [--actor orch\|operator]` | approval JSON | `approved` / `duplicate` |
| `orchd intake claim --draft D --version N` | – | `claimed` |
| `orchd intake receipt [--file F\|-] [--actor connector\|operator]` | receipt JSON | `recorded` / `duplicate` |
| `orchd intake cancel --draft D [--actor]` | – | `cancelled` / `duplicate` |

## Event (submit)

Required: `version` (=1), `provider` (`email|slack|line`), `account`, `event_id`, `source_ref`, `source_time`
(unix seconds), `summary` (≤1000 chars). Optional: `company` (default = account), `thread_ref`, `owner_orch`.
Any other field (including `producer`, `sender`, `body`) is rejected `unknown_field`. No full text is accepted.

Unique key: `(provider, account, event_id)`. Same key + same content → original receipt, `status=duplicate`.
Same key + different content → `conflict`, nothing overwritten. `owner_orch` must be a registered Orch
(`unknown_owner`); omitted = `owner_state: unassigned`, never auto-assigned.

Receipt: `status, receipt_id, provider, account, event_id, received_at, state (received|acked), owner_orch,
owner_state (assigned|unassigned), content_sha256`. The scanner advances its checkpoint only after this receipt.
`ack` is a separate later stage (Orch processed it) and requires an owner.

## Draft → approval → send

States: `pending_approval → approved → sending → sent | failed | uncertain`; also `cancelled`, `superseded`.
Draft input: `{draft_id?, receipt_id?, provider, sender, target, source_ref, level (L0-L2, default L2), template?,
summary, body, expires_at}`. A changed draft is a new `version`; older versions become `superseded` and their
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
  sources: [{provider, account, company, received, unacked, last_received_at}],
  items:  [{receipt_id, version, provider, account, company, event_id, source_ref, thread_ref, source_time,
            received_at, producer, summary, owner_orch, owner_state (assigned|unassigned|owner_stopped),
            state, acked_at, acked_by, classification, level, todo_id, issue_ref, project,
            retention (ok|expiring|expired|acked), trust: "third_party_data", draft_refs: [{draft_id, version}]}],
  drafts: [{draft_id, version, receipt_id, provider, sender, target, source_ref, level, template, summary,
            body_sha256, expires_at, state, created_by, created_at, updated_at, connector_receipt_id,
            outcome_detail, is_latest, expired, approval_id}]}
```

`available: false` (DB without the intake tables) means "not connected", never zero items. `revision` is the max
`intake_log.seq`; it changes on every state change in the same DB file, so the existing DB/WAL watch refreshes SSE.
Bodies of events and drafts are never projected. Items are the newest 500.

## Not in W1

Source adapters (W2/W3), MAP/TODO classification logic (W4), dashboard UI (W5), runner and launchd (W6), pilot (W7).
Retention purge (7-day/90-day) is not implemented; the snapshot only warns. Pre-send re-checks (did Nat already
reply, did the source change) belong to the connector before `claim`.
