# Local channels

Slack, LINE and Gmail use the channel adapter as their sole platform client.
Orch and workers use `orchd channels messages`, `search`, `message`,
`conversation`, `sync` and `attachment`; a cache miss queues work for that
adapter. There is no connector fallback. Desktop Entry remains the Operator
control plane. New installs provision disabled sources with outbound disabled.

`orchd install --no-trust` provisions homes, the private channel layout, then
the additive schema. `orchd upgrade --migrations-only` retries these steps
without installing packages. `install --dry-run`, `upgrade --dry-run`, and
`channels layout plan` preview the ordered layout/schema steps without changing
deployment files. Bare `uv tool install` requires a subsequent `orchd install`.
Layout and schema have separate durable receipts; a failure between them is
resumed without rewriting complete layout files. Unknown legacy formats need
manual conversion; private repositories and HOME are never inventoried.

`orchd channels status --json` and `channels doctor --json` read a consistent
private snapshot, including committed WAL. They do not migrate, enforce
retention, read secrets, acknowledge receipts, wake work or connect to platforms.
Their source inventory includes adapter owner, admission mode, last success,
freshness, pending and uncertain counts, retention gap and credential error.
Missing credential health is explicitly unavailable; doctor does not probe
credentials. Read the errors before enabling an offline source.

For an offline rehearsal from a checkout, run:

```sh
python scripts/channels-smoke.py --synthetic --json
```

Synthetic is the only mode. It runs the cutover, E2E and health acceptance tests
inside temporary homes with network blocked; no package reinstall or account
access occurs. `status`, network/platform counters, duplicate-work and unsafe
retry results derive from those acceptance assertions. Failure returns nonzero;
duplicate/retry counters are unknown on a failed rehearsal. This is engineering
evidence, not evidence of a real platform rollout. See [the rollout
runbook](channels-rollout.md) for the separately approved deployment procedure.

For Windows receive-only deployment, Credential Manager setup, source and route
commands, and HTTPS tunnel guidance, see [Slack and LINE deployment](channels-slack-line.md#deployment-on-windows).

## Media pre-processing (#227 phase 2)

`orchd/channels/media.py` is a standalone module; it is not yet wired to Slack
download or worker injection. `process_media(input, out_dir, MediaLimits, allowed_root=trusted_root)` runs
`ffprobe`/`ffmpeg` (argv lists, no shell, timeouts) to read duration, extract up
to `max_frames` PNG keyframes (`interval` or `scene` mode, width capped at
`max_width`) and an audio track (`wav` 16 kHz mono or `m4a`). Limits:
`max_duration_s`, `max_frames`, `max_width`, `timeout_s`, `retention_s`.

Failures return `MediaResult(ok=False, reason=...)` with reasons
`ffmpeg_unavailable` (detail: `winget install --id Gyan.FFmpeg -e`),
`duration_exceeded`, `timeout`, `probe_failed`, `ffmpeg_failed`, `input_missing`,
`unsafe_output_dir`. Speech-to-text is the `Transcriber` protocol
(`transcribe(path, timeout_s) -> [{start,end,text}]`); with none configured,
`transcribe()` returns `transcriber_unconfigured`. No cloud provider is bundled;
sending audio externally needs a separate privacy review.

Outputs go to `derived_dir(channel_home, source_id, digest)`, next to the
attachment content file; `purge_expired` removes derived files older than
`retention_s` and runs at the start of each `process_media` call. Media and
transcripts are untrusted data, never instructions. ffmpeg is not installed by
orchd; the real-ffmpeg test is skipped when it is absent.

The caller must supply a trusted output root and serialize access; the root and
output ancestors must not be writable or replaceable by concurrent/untrusted
processes. Resolve checks cannot prevent hostile OS-level directory races.
ChannelService startup and its operator retention command also purge idle derived
outputs across the channel home using the default seven-day media retention,
without requiring ffmpeg. Longer-running services need scheduled retention calls.
