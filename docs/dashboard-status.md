# Find the ORCH Dash Page

Run `orchd dashboard` or say **Dashboard** or **orchd dashboard** in the
Desktop Interface. Both read the current orchd profile. Use
`orchd dashboard --json` for the same fields returned by the Interface tool.
The CLI reads only the two JSON files below. A running Interface MCP session
uses the same file lookup without querying its open task database, relaying a
message, or triggering a sync.

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


## Read and refresh from Desktop Interface

- Say **讀 Dashboard** to read the current cards from the bound Page.
- Say **立刻更新 Dashboard** to run the local publisher once, bypassing its
  schedule. The tool reports whether it updated the Page or verified that it
  was already current. A status query still reports the last verified receipt.
- Answer a question in Interface as usual.

Both actions use an authenticated Codex CLI with the Pages plugin and Node.js
on the Interface MCP host. Put the Pages publisher's profile `config.json`
under `ORCHD_CONFIG_DIR/pages-publisher/`. Its `pageId`, `state`, and
`profileConfig` must match the active Page and profile. It also supplies
`fileReference`, `python`, `orchd`, and `watcher` for the snapshot pipeline.
Use the Python interpreter from the orchd installation so the receipt helper
can import orchd's file lock.
When a periodic sync is enabled, use the same installed
`orchd/dashboard_publisher.cjs` source and `orchd/dashboard_receipt.py` helper
for both modes. Let any in-flight run of the old periodic job finish, then
replace its copies before using on-demand sync. Both modes check the Page
revision before publishing and coordinate receipt updates. Set `node` and `codex` to executable
paths if the MCP host cannot find them on its PATH. The actions use the
account signed in to that Codex CLI. For a write, the Pages API must confirm
owner-only access.


## Assign a question to a project

When a decision belongs to a different project from its worker's repository,
call `ask_nat` with `project` set to the project slug. The CLI equivalent is
`orchd entry ask --project PROJECT --text "…"`. To correct an existing open
question in place, first inspect its version and state with
`orchd entry-question show ID`, then use:

```sh
orchd entry-question set-project ID --project PROJECT --expected-version VERSION --expected-state queued
```

Use the question's actual state. This changes only the grouping metadata.
The Dashboard groups version 2 snapshot questions by their explicit project
first, then by the worker's repository, then under unassigned. The snapshot
and publisher still read question IDs, queue states, and task links from the
orchd DB.

For an existing Pages publisher that loads `orch-dash-project.py`, keep any
profile-specific display names in that file and delegate its projection to
the installed orchd module. The periodic and on-demand publishers can keep
using the same watcher and Page:

```python
from orchd.dashboard_projection import project as project_snapshot

PROJECT_NAMES = {}  # Keep the existing profile's display-name mapping here.

def project(snapshot):
    return project_snapshot(snapshot, project_names=PROJECT_NAMES)
```
