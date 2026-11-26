"""Read the Orch, Desktop entry and channel ownership recorded in this profile."""
import time

from . import stats


def read(path, orch_id=None):
    with stats.open_snapshot(path) as snapshot:
        con = snapshot.con
        con.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        archived = "archived_at" if any(
            row["name"] == "archived_at" for row in con.execute("PRAGMA table_info(orchs)")
        ) else "NULL AS archived_at"
        orchs = {
            row["id"]: dict(dict(row), desktop=[], listeners=[], channels=[])
            for row in con.execute(f"SELECT id,kind,stopped_at,{archived} FROM orchs ORDER BY id")
        }

        def add(owner, key, item):
            if owner not in orchs:
                orchs[owner] = dict(id=owner, kind=None, stopped_at=None, archived_at=None,
                                    desktop=[], listeners=[], channels=[])
            orchs[owner][key].append(item)

        for row in con.execute("SELECT id,orch_id,thread_id FROM entries ORDER BY id"):
            add(row["orch_id"], "desktop", dict(entry_id=row["id"], thread_id=row["thread_id"]))

        if "interfaces" in tables:
            now = time.time()
            for row in con.execute("SELECT name,orch_id,disabled,expires_at FROM interfaces ORDER BY name"):
                state = "disabled" if row["disabled"] else "expired" if row["expires_at"] <= now else "registered"
                add(row["orch_id"], "listeners", dict(name=row["name"], state=state))

        channels_available = {"channel_sources", "channel_routes"} <= tables
        if channels_available:
            for row in con.execute(
                "SELECT r.id AS route_id,r.owner_orch,s.provider,r.source_id,r.conversation_ref,r.project_ref,"
                "r.mode,s.enabled AS source_enabled FROM channel_routes r JOIN channel_sources s ON s.id=r.source_id "
                "WHERE r.active=1 ORDER BY s.provider,r.id"
            ):
                route = dict(row)
                owner = route.pop("owner_orch")
                route["source_enabled"] = bool(route["source_enabled"])
                add(owner, "channels", route)

        if orch_id is not None:
            if orch_id not in orchs:
                raise ValueError(f"unknown Orch {orch_id}")
            orchs = {orch_id: orchs[orch_id]}
        return dict(orchs=list(orchs.values()), native_channels_available=channels_available,
                    external_listener_routes_available=False)


def render(report):
    lines = []
    for orch in report["orchs"]:
        status = ("unregistered" if orch["kind"] is None else "archived" if orch["archived_at"] is not None
                  else "stopped" if orch["stopped_at"] is not None else "registered")
        lines.append(f"{orch['id']} [{status}]")
        for entry in orch["desktop"]:
            thread = entry["thread_id"] or "no chat yet"
            lines.append(f"  Desktop {entry['entry_id']} (chat {thread})")
        for listener in orch["listeners"]:
            lines.append(f"  Listener {listener['name']} [{listener['state']}]")
        for channel in orch["channels"]:
            project = f" → {channel['project_ref']}" if channel["project_ref"] else ""
            state = channel["mode"] if channel["source_enabled"] else "source disabled"
            lines.append(f"  Channel {channel['provider']} {channel['source_id']}/{channel['conversation_ref']}"
                         f"{project} [{state}, route {channel['route_id']}]")
        if not any(orch[key] for key in ("desktop", "listeners", "channels")):
            lines.append("  No bindings recorded in this DB.")
    if not lines:
        lines.append("No Orchs registered in this DB.")
    if not report["native_channels_available"]:
        lines.append("Native channel route tables are unavailable in this DB.")
    lines.append("For legacy Slack per-channel and LINE per-group routing, check the external listener configuration.")
    return "\n".join(lines)
