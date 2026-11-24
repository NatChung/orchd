# Find the ORCH Dash Page

Run `orchd dashboard` or say **Dashboard** or **orchd dashboard** in the
Desktop Interface. Both read the current orchd profile. Use
`orchd dashboard --json` for the same fields returned by the Interface tool.
A lookup does not open the task database, relay a message, or trigger a sync.

## Bind a Page and report sync status

Create `dashboard.json` in `ORCHD_CONFIG_DIR` (default `~/.config/orchd`).
Use the canonical Page link from ChatGPT Space.

```json
{"page_url":"https://chatgpt.com/space/page_example"}
```

The Pages publisher atomically replaces `dashboard-sync.json` in the same
profile's `ORCHD_HOME` (default `~/.local/share/orchd`). Write `synced` after
reading back the expected Page state. Report `failed` for a failed attempt
and retain its preceding successful timestamp if one exists. Use offset-aware
ISO 8601 timestamps.

```json
{
  "page_url": "https://chatgpt.com/space/page_example",
  "status": "synced",
  "last_attempt_at": "2026-11-24T02:00:00Z",
  "last_synced_at": "2026-11-24T02:00:00Z"
}
```

Before the first success, `last_synced_at` can be null on a failed receipt.
A profile with no configured Page reports `unconfigured`. A configured Page
without a matching receipt reports `never_synced`. Keep one publisher for
each Page and profile pair.

Fresh `orchd init` pre-approves the read-only tool. Existing Interfaces receive
the managed rule and tool approval through the home migration in the upgraded
binary. Reload the Interface chat to pick up the new tool. The publisher has
its own lifecycle and Pages access.
