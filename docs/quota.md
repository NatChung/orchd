# Quota observation and dispatch

Explicit `sol` / `sonnet` and omitted model retain their existing behavior.
Opt in with `model="auto"`; batch items default to auto. The routing policy only
reads the shared cache and task failure records; it never acquires quota.
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
Codex refresh is single-flight with a nonblocking file lock and a 30-second
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
  and during idle time under the shared 30-second attempt TTL. They consume
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

Explicit/omitted-model dispatch has `applied=false` and policy `quota-shadow-v1`.
Auto dispatch applies this same policy as described below. Known capacity
uses a provisional 5% margin and the minimum remaining-percentage / seconds-to-reset
score across all observed windows/buckets. Similar scores favor sol. This has no
task-effort estimates or running-task reservations, so it is for calibration only.
Unknown ordinary quota falls back to a labelled sol preference; known exhausted
windows suggest waiting. App-server permits only sol. A review of `verifies`
requires the other provider; unknown author or unavailable/incompatible required
provider suggests waiting, never the same provider. A `task_type=review` without
`verifies` has no author evidence and suggests waiting. The policy uses the
author task's current stored model; mixed-author retry histories require Orch
to explicitly choose an independent reviewer.

Observation/storage errors yield an unavailable shadow and cannot change or fail
the launch. Shadow does not authorize work, guarantee capacity or change the
caller's explicit model, even when it recommends waiting.

## Applied auto policy and parallel tasks

`dispatch(model="auto", ...)` calls the same **quota-shadow-v1** policy with
`applied=true`. Known usable capacity retains the original minimum
`(95 - used_percent) / seconds_to_reset` score across windows, picking the highest
score; ties prefer sol. Thus known-capacity routing may choose sonnet even before
sol is full, as the existing shadow policy recommends. The **95% threshold** is
unchanged. An observed sol rate-limit failure excludes sol for **10 minutes**.
Unknown/stale/unreadable quota does not block ordinary tasks: prefer sol absent
known exhaustion/recent failure, or sonnet when sol is full. Every applied choice
appends its reason to `model_reason`, returned by dispatch, list_open and inbox.
If both providers are full, choose the non-cooldown provider with the lowest
maximum known used percentage. Ties or no such evidence fall back to sol.
This is best-effort, not a quota reservation or a guarantee of equivalent tokens.
An explicit app-server backend constrains auto to sol; leave backend omitted to
allow provider switching at dispatch. For independent review, explicitly choose
the other provider or supply `verifies`; auto preserves the shadow's cross-provider
constraint. A review with an unknown author must supply author evidence or an
explicit model. Explicit/omitted model dispatch keeps its unapplied shadow.

Failed Codex app-server turns with rate/usage-limit errors notify the Orch once
per turn. Exec wrappers inspect only structured `turn.failed` errors in the last
64 KiB of that turn's log, guarded by its current pid. They retain a provider
failure event for the cooldown, even after inbox is read. **Auto tasks automatically
retry with sonnet**, using the existing stop/delivery safeguards on the same
worktree. Explicit/omitted-model tasks only notify the Orch. A detached helper
avoids stopping its own exec wrapper or app-server supervisor. Under the existing
task lock it checks the expected model/job/session/generation/turn, claims a
durable `auto_retry_count`, then retries. Each task gets **at most one automatic
attempt**, including failed attempts; duplicates, manual replacement, newer
native turns and closed tasks cannot launch another automatic worker. The queued
inputs wait for the replacement. Inbox progress reports the rate-limit reason,
scheduling, success, failure or supersession. A failed helper/stop/trust/spawn
requires Orch follow-up; it preserves existing work rather than looping.
The replacement prompt explicitly requires checking the existing worktree and
delivery history, continuing its results, and avoiding completed work or already
sent actions. Workers retain the existing ask/approval rule for every new outward
action, keeping replay risk controlled while automatic retry continues the task.
App-server reconciliation also catches missed failed completions, and definitive
rate-limit RPC rejections are handled outside the delivery lock. Startup failure
without a successfully started turn only notifies, retaining the existing dispatch
failure behavior.

These applied fallbacks/cooldown and automatic retry are deliberate extensions
to the original observation-only shadow. Known-capacity scoring and quota
collection are unchanged; there is no second selection algorithm.

`dispatch_batch(tasks=[...])` takes **1–4 independent dispatch specifications**.
Each gets a distinct worker and worktree, including items in the same repo.
Launches happen in input order; workers continue concurrently without waiting for
another worker's completion. Each item independently reads the cache and defaults
to auto. Results contain an index and success/error per item plus a shared
`group_id`. Use `list_open(group_id=...)` and `inbox(group_id=...)` to inspect a
group; filtered inbox leaves other groups unread. Successful items survive later
failures: retry only failed items after inspecting group status. Excess items are
rejected before launch and must be submitted by the Orch in later batches.
There is no persistent queue, global worker limit, or quota-derived concurrency
reservation in this version. Avoid overlapping edits and dependent items.

The prior task 22536b35 design was not present in this checkout or its available
Git history. This implementation keeps the P1 cache/TTL/shadow interface and
adds an explicit opt-in rather than changing configured default model selection.

### Isolated routing evidence

Run `python3 scripts/routing-smoke.py` with the repo dependencies available.
It creates a temporary quota/task store and synthetic Git repo, invokes the
checkout's MCP handlers, launches real OS children in real Git worktrees, proves
their simultaneous liveness, and cleans up. Provider launch/message adapters are
fixtures; this does **not** verify live Claude/Codex inference or production
installation. A run on this branch produced:

```text
a: sol full -> claude-sonnet-5-5; quota-shadow-v1 applied=true;
   reason: known capacity with 5% margin; remaining capacity/reset-time score
b: quota unknown -> gpt-6.1-sol; reason: quota unknown/stale; fallback
c: group b8105e07c6c9a; pids [53019, 53024]; simultaneous_alive=true;
   distinct_git_worktrees=true; grouped_tasks=2
d: automatic rate-limit retry -> claude-sonnet-5-5; auto_retry_count=1;
   same_worktree=true; replacement_alive=true
PASS: isolated state, real child processes and Git worktrees; provider adapters are fixtures, no live LLM inference
```

The unittest suite includes the process smoke plus policy, metadata, explicit
model preservation, TTL/stale fallback, rate-limit notification and same-worktree
automatic provider retry/once-only cap, batch defaults/limits/partial failure, and filtered group inbox.
It also covers PR #36's failed Claude `/usage` probe overwriting known quota with
unknown: auto still falls back and starts sonnet when sol is exhausted.
Required full command: `python3 -m unittest discover -s tests`.

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
  tries refresh during idle time, with the existing single-flight 30-second attempt TTL, leaving 30 seconds before observations
  expire to absorb RPC latency.
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
probes returned known subscription windows without model inference. A native
Codex account RPC was also refreshed at idle samples over 63 seconds; all three
samples stayed known with fresh receipt timestamps, without a model turn. Tests cover
exec thread isolation and receipt age, forced app-server turn reads and idle
TTL, native Claude parsing/year rollover, unknown/timeout behavior and worker
hook wiring. No install, restart or merge is part of this change.
