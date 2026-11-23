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

`orchd/channels/media.py` supplies the bounded processing used by the owner-only
media MCP and worker dispatch below. `process_media(input, out_dir, MediaLimits, allowed_root=trusted_root)` runs
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


## Orch media reads and worker dispatch (#227 phase 3)

After `channels_attachment_request(attachment_id=...)` has been fulfilled by the
adapter, the owner Orch can call `channels_attachment_media(attachment_id=...)`.
It checks the server-side source ACL, exact message owner, retention, size and
SHA-256 before returning anything. Unowned messages, other Orchs and workers
cannot use this media API. It never connects to Slack or reads platform tokens.
The existing raw `channels_attachment_read` API keeps its existing contract.

The result has `untrusted: true`, `status`, `images` (local `path` and `mime_type`),
`transcript` (segments with `start`, `end`, `text`) and `transcript_status`.
Images are returned directly; video gets bounded PNG frames and an audio-track
transcript; audio skips frame extraction. With no transcriber configured, status
is `transcriber_unconfigured` (轉錄未設定); silent video reports `no_audio`.
Missing ffmpeg, timeouts, invalid or overlong media return explicit unavailable
reasons. Non-media files return `unsupported_media_type`.
MCP includes inline image blocks for PNG/JPEG/GIF/WebP, preceded by untrusted
metadata. Other image formats (including HEIC) return their path with
`unsupported_image_format`; no implicit conversion or image injection occurs.

`dispatch` and each `dispatch_batch.tasks` item accept optional
`attachment_refs: [{"attachment_id": "ca_..."}, {"message_id": "cm_..."}]`.
Each item has exactly one ID; message references expand to that message's
attachments. References are deduplicated; at most eight input references and eight
expanded attachments are accepted, with aggregate original size at most 25 MiB.
ACL and aggregate size are checked before preprocessing or worker creation.
Unavailable attachments remain explicit in the worker's media metadata; the Orch
can request/download them and dispatch again. Batch failures remain per item.

- Codex app-server: the initial `turn/start.input` includes `localImage` items
  (the RPC equivalent of `local_image`) plus untrusted metadata and transcripts.
- Claude: the task includes local paths and asks the worker to use its Read tool,
  reporting unreadable or unsupported images explicitly.
- Codex exec: explicitly reports a **text-only downgrade**, supplies paths and
  transcripts, and forbids claiming visual inspection without a successful image
  tool read. Unsupported formats receive paths and an explicit reason on all
  backends. Media injection is limited to the initial dispatch turn. A fresh retry
  without the original session history must report missing media and request a
  new dispatch with attachment_refs; it must not claim visual inspection.

The service accepts `media_limits=MediaLimits(...)` and `transcriber=...` as trusted
integration configuration points; neither is client-supplied MCP input. No cloud
transcriber or credential setting is bundled. A provider must implement the
existing Transcriber timeout contract. Default duration, frames, dimensions,
processing timeout and derived retention remain the phase-2 settings (600 seconds,
8 frames, 1280×720 / 921600 pixels, 60 seconds, seven days). Original attachments
retain the source retention policy. Derived transcripts are returned in memory and are not copied into persistent
task instructions. The dispatch audit stores only attachment references; backend
session histories may retain delivered media under their own retention rules.

Processing is serialized per source by a local nonblocking file lock; contention
returns `media_busy`. Paths are confined to channel home. Message deletion/expiry
removes derived files as well as cached originals. Retention removes idle derived
outputs without requiring ffmpeg. The trusted-directory/race assumptions above
still apply; workers run locally with their existing permissions, not a new file
sandbox. Media, OCR and transcript text are data, never instructions, approvals
or authority. Workers receive no adapter token or credential references.

Verification covers synthetic media subprocesses, owner ACL, MCP image blocks,
Claude/exec dispatch, batch propagation and the app-server `localImage` RPC input.
The RPC spelling was checked against the installed Codex-generated JSON schema.
This does not establish live Slack download-to-model visual judgement, Claude Read
support on every platform, or real transcription quality; those require an
explicit deployment acceptance run. ffmpeg is never installed automatically.
