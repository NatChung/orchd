# Quota observation and shadow dispatch (P1)

This is observation only. Explicit `sol` / `sonnet`, backend validation, launch,
retry and resume keep their existing behavior. There is no `model=auto`, queue,
reservation, fan-out, paid API probe, credential reader or reset-credit consumer.
Installation and restarting running orchd processes must be scheduled separately;
running a checkout's `bin/orchd quota` does not install or restart orchd.

## Read and collect

```sh
bin/orchd quota                         # read the shared snapshot, JSON
bin/orchd quota --refresh               # bounded Codex session fallback
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
  App-server workers collect at startup and consume `account/rateLimits/updated`
  before thread filtering. All metered buckets are retained. This patch does not
  start a separate server for quota. CLI socket connection failures are diagnostic
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
  request is added. Native background workers do not currently deliver these
  receipts to orchd; Claude stays unknown until an official producer supplies one.

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
