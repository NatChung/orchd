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
