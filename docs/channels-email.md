# Gmail channel cutover (PR3)

The agent-facing `channels messages`, `search`, `message`, `conversation`, `sync`, `sync-status`, and `attachment request/read` commands and their `channels_*` MCP equivalents use the local service. They never open a platform connection. A missing attachment is queued for the adapter; a search outside the local index is `sync_required`. Third-party content remains untrusted. `adapter_status`, `last_synced_at`, and `freshness` describe the last recorded heartbeat and synchronization, not a delivery or read receipt.

The receive-only integration is `EmailAdapter` plus `AdapterRunner`; instantiate it for a provisioned, enabled Gmail source with a source-scoped OS secret-store provider and a bounded activation time. The adapter resolves the `read` reference through the PR1 secret-store interface; the provider returns the OAuth authorized-user credential bytes. The real transport lazily loads the `channels-email` extra and requests only `gmail.readonly`. The interface requires a deployment-provided OS-store implementation; no OS-store provider, account, OAuth authorization, or runner service is provisioned by this change. Do not put credential bytes in the repository, configuration, command arguments, or logs. Sending, marking read, modifying labels, and deletion are disabled.

## Inventory and cutover checklist

1. For each source, inventory existing mailbox polling processes, scheduled jobs, and agent connector tools, and identify its host, source grant, activation/backfill window, and current durable watermark. This repository contains no prior Gmail connector tool; inventory external deployments separately.
2. Stop the old polling and connector tools for that source. Preserve their watermarks and deduplication evidence; configure the new source and bounded backfill using synthetic fixtures before any actual activation.
3. Start exactly one email adapter and check local sync job, freshness, and adapter health. Verify duplicate replay against the retained ledger. Real account authorization and activation require separate approval.
4. For rollback, stop the new adapter first, retain the new message and checkpoint ledger, reconcile the incremental gap, and only then start one old receiver. Local query APIs do not fall back to a direct connector.

All attachment content stays in the source's controlled content directory, named by digest, and access goes through source and owner ACL checks. Retention and tombstones use the existing PR2 policies. A database or directory rollback cannot undo external effects or safely discard newly received mail.
