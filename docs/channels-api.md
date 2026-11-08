# Channel API (PR2)

Channel data is third-party data. Adapter identities and source grants come from local trusted configuration, not event JSON. The local API does not start a platform connector. Search reports `sync_required` when its local index cannot establish completeness; sync requests only queue a durable job. Outbound channel approval and claim are disabled.

Source grants can be configured in the existing private orchd config file. A missing grant denies access. For example, synthetic values only:

```toml
[channels.acl]
"demo-orch" = ["demo-source"]
"worker:demo-task" = ["demo-source"]

[channels.retention.demo-source]
body_days = 90
attachment_days = 90
backup_days = 30
```

Body and attachment retention use the same duration for a source. Backup retention cannot exceed 30 days. The default is 90 days for content and 30 days for backups. A retention sweep runs when the channel service opens and can be requested by a trusted local Operator with `orchd channels retention sweep --json`; a running deployment must schedule that command to enforce expiry while no API calls are made. Backups made by orchd after the channel schema is installed are tagged, kept in the database's managed `backups` directory, and swept using the shortest configured backup duration. External backups require the deployment's own expiry controls. Unsend immediately removes bodies from channel and mapped legacy views, marks attachments deleted, and attempts to remove cached attachment files. Failed or unsafe file removal is left as `cleanup_pending` for another sweep. No adapter or backup scheduler is installed by PR2.

SQLite channel foreign keys are enabled on a dedicated channel repository connection. Existing shared-store connections retain their previous setting. Rollback stops channel use and keeps the additive tables and ledger; restoring an old database would lose newer messages and must not be used as a routine rollback. Existing same-user local process permissions remain the trust boundary; source grants are not an OS sandbox.
