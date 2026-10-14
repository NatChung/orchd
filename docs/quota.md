# Quota observation and shadow dispatch (P1)

This is observation only. Explicit `sol` / `sonnet`, backend validation, launch,
retry and resume keep their existing behavior. There is no `model=auto`, queue,
reservation, fan-out, paid inference probe, credential reader or reset-credit consumer.
Installation and restarting running orchd processes must be scheduled separately;
running a checkout's `bin/orchd quota` does not install or restart orchd.

## Read and collect

```sh
bin/orchd quota                         # read the shared snapshot, JSON
bin/orchd quota --refresh               # bounded Codex session fallback
bin/orchd quota --probe sol             # temporary Codex app-server; quota read only
bin/orchd quota --probe sonnet          # native Claude /usage; zero inference turns required
bin/orchd quota --endpoint /path/to/existing/app-server.sock
bin/orchd quota --ingest claude-statusline < official-statusline-receipt.json
bin/orchd quota --ingest claude-sdk < official-sdk-event.json
```

The Orch MCP exposes `quota_status`, including structured JSON; it reads the
same snapshot without registering the caller or consuming inbox messages. The
Desktop entry's tool allowlist is unchanged. Dispatch returns `quota_shadow` via
MCP and also records it in the existing `dispatch` message for audit.

The shared snapshot is `$ORCHD_HOME/quota.db` (default
`~/.local/share/orchd/quota.db`), separate from the task DB. Concurrent writers
upsert each bucket transactionally; older receipts cannot replace newer ones.
Codex refresh is single-flight with a nonblocking file lock and a 60-second
attempt TTL. Reads never create or migrate a DB. Observations older than 60
seconds are unknown; older than 300 seconds are marked stale. Missing/invalid
percentages, durations, resets, future observation timestamps, and passed resets
are unknown. The old reset and source remain visible, but `used_percent` becomes
null. A reset never implies zero usage. A missing Codex secondary is not an
invented 5-hour window: some plans report only a weekly primary.

This version supports the machine's single local signed-in subscription scope.
Use separate `ORCHD_HOME` and collectors for different accounts; it cannot detect
account switches and does not combine account identities or API budgets. Clear
or isolate observations before changing the signed-in account. Percentages for
different providers are not equivalent token quantities.

## Official sources and limits

* [Codex app-server](https://learn.chatgpt.com/docs/app-server): existing initialized
  RPC connections use `account/rateLimits/read` (3-second response timeout).
  App-server workers collect at startup, at each matching turn start/completion,
  and during idle time under the shared 60-second attempt TTL. They consume
  `account/rateLimits/updated` before thread filtering. All metered buckets are
  retained. Explicit `--probe sol` starts a temporary private app-server, reads
  the account RPC, and terminates it without starting a thread or model turn. CLI socket connection failures are diagnostic
  errors; read RPC failure falls back to sessions.
* Codex session fallback: inspect only `event_msg` / `token_count.rate_limits`
  within the last 256 KiB of the 20 most recently modified session files. Preserve
  the event's timestamp, not the time of reading the file. No transcript content
  or token totals enter the snapshot. Bounded sampling can miss observations,
  in which case data stays unknown. The directory inventory still traverses the
  session tree; dispatch itself only reads the shared cache and never scans it.
* [Claude statusline](https://code.claude.com/docs/en/statusline): ingest official
  stdin JSON `rate_limits.five_hour` and `seven_day`, each independently nullable.
  This is a receipt collector, not an automatic statusline configuration change.
  An approved future statusline wrapper can pipe a copy into this command while
  preserving the existing display. Do not substitute context-window percentages.
* [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python#ratelimitinfo):
  ingest serialized `RateLimitEvent` (`rate_limit_info` with `rate_limit_type`,
  `utilization`, `resets_at`). Missing utilization is unknown. Sonnet weekly is
  retained separately; opus and overage are ignored. No SDK dependency or paid
  request is added. SDK receipts remain supported independently of the native worker collector below.

## Shadow policy

Every dispatch has `applied=false` and policy `quota-shadow-v1`. Known capacity
uses a provisional 5% margin and the minimum remaining-percentage / seconds-to-reset
score across all observed windows/buckets. Similar scores favor sol. This has no
task-effort estimates or running-task reservations, so it is for calibration only.
Unknown ordinary quota falls back to a labelled sol preference; known exhausted
windows suggest waiting. App-server permits only sol. A review of `verifies`
requires the other provider; unknown author or unavailable/incompatible required
provider suggests waiting, never the same provider. A `task_type=review` without
`verifies` has no author evidence and suggests waiting. Mixed authors and retry
history need P2 policy inputs before auto can be enabled.

Observation/storage errors yield an unavailable shadow and cannot change or fail
the launch. Shadow does not authorize work, guarantee capacity or change the
caller's explicit model, even when it recommends waiting.

## Local evidence

Ported from archived commit `1b71ecc` onto public main `5ec478c`.
Red: quota import was missing; after adding the adapter, integration tests failed
for the absent CLI command, dispatch metadata and account event collection.
Green: all 11 quota tests passed. The public config model/backend tests and issue
link tests run in the full unittest suite; quota's FakeRuntime uses explicit
`model=sol, backend=exec` so it cannot accidentally launch an app-server.
The public MCP tools are retained, and `quota_status` is excluded from caller
registration just like `list_orchs`.

Local checkout `bin/orchd quota --refresh` observed a Codex weekly window: used
44%, length 604800 seconds, reset 1792473781, source
`codex.session.token_count`, observed 1791957748.73, age 3.175 seconds.
Claude was unknown: no official receipt. This is an observation at the time of
the run, not a present capacity guarantee.
Live app-server read/updated and native Claude headless receipt delivery were not
verified against running services; adapter integration is tested with fixtures.
No install, restart, merge, global configuration edit or paid inference occurred.

## Worker collection (quota acquisition only)

The archived task 22536b35 design was not present in this public checkout or its
available git history. This implementation extends the existing P1 observation
contract above; dispatch selection and `quota_shadow` are unchanged. All previous
`quota_status` keys remain; providers with observations additionally expose
`unknown_reasons`. The `state` enum remains `known` / `unknown`; `stale` is a
window reason after 300 seconds, and `expired TTL` is a reason after 60 seconds.

* **Codex app-server:** force a read on matching `turn/started` and
  `turn/completed`, even if another worker just refreshed. The supervisor also
  tries refresh during idle time, with the existing single-flight attempt TTL.
  Failed reads fall back to bounded session receipts without changing their
  original timestamps. These account reads cost process/network work, not model
  tokens. A disconnected/stopped supervisor cannot maintain freshness.
* **Codex exec:** the existing shell that flushes queued answers now collects
  quota between CLI exit and flush. It reads `thread.started` from that turn's
  log, then only that thread's rollout `token_count.rate_limits`. Start and every
  resume use the same wrapper. Worker errors still flush; telemetry errors are
  ignored by the wrapper. Token totals never become quota. An idle exec worker
  has no process: use `quota --probe sol` for an authoritative fresh read; the
  session fallback alone cannot make old data fresh. The probe starts a temporary
  private server and tears it down, with a 10-second socket deadline and a
  3-second read deadline (RPC initialization retains its transport timeout).
* **Claude native workers:** session-only `--settings` adds command hooks for
  `SessionStart`, `UserPromptSubmit`, `Stop`, and `StopFailure`. Each runs the
  checkout/installed collector against the same explicit `ORCHD_HOME`. Existing
  statusline display and global settings are untouched. The collector launches
  `claude -p /usage --output-format json`, disables hooks to prevent recursion,
  disables tools/session persistence, and excludes configured MCP servers.
  Timeout is 15 seconds; hook timeout is 25 seconds. Failures do not block the
  worker or feed stdout back into its prompt. Each hook adds CLI startup and a
  subscription-status network read; there is no periodic idle Claude process.
  Use `quota --probe sonnet` to refresh an idle worker's subscription.

[Claude's built-in `/usage`](https://code.claude.com/docs/en/interactive-mode)
reports subscription usage. On locally verified Claude Code 2.1.299 it works in
print mode as a local command (`local_command=usage`, `num_turns=0`,
`duration_api_ms=0`, zero cost and zero token usage). The collector requires that
local-command evidence before accepting the text. This is a version-dependent
CLI display adapter, not an undocumented credential/API reader. Unsupported
versions, auth failures, timeouts, nonlocal results and unsupported locales are
stored as explicit unknown observations with the source and reason; no model
answer, context usage or approximate local-session breakdown is accepted.

Only the `Current session` and `Current week (all models)` percentages enter the
five-hour and seven-day windows. Extra per-model rows are not relabelled as
Sonnet. The supported reset display includes English month/day, 12-hour time and
an explicit IANA timezone. A missing year is resolved only to a unique future
reset within the reported window duration; missing/ambiguous/past resets are
unknown. Displayed minutes are rounded down conservatively and carry the added
`reset_precision_seconds=60` field. A reset never implies zero usage.
The statusline and SDK adapters remain available for exact structured receipts.

[Claude command hooks](https://code.claude.com/docs/en/hooks) provide per-session
and per-turn collection without altering model dispatch. Managed policy or
`disableAllHooks` can suppress worker hooks; in that case no receipt is refreshed
and TTL/stale rules still apply. Explicit probes are available for diagnosis.

Local branch validation uses a separate temporary `ORCHD_HOME`. Both native
probes returned known subscription windows without model inference. Tests cover
exec thread isolation and receipt age, forced app-server turn reads and idle
TTL, native Claude parsing/year rollover, unknown/timeout behavior and worker
hook wiring. No install, restart or merge is part of this change.
