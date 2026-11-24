"""Read the configured Dashboard Page and the publisher's last verified receipt."""
from datetime import datetime, timezone
import json
import re

from . import paths, store


PAGE = re.compile(r"https://chatgpt\.com/space/page_[A-Za-z0-9_-]+")


def _object(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"Invalid {path.name} JSON") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Dashboard sync receipt requires timezone-aware timestamps")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        pass
    raise ValueError("Dashboard sync receipt requires timezone-aware timestamps")


def status():
    result = dict(page_url=None, status="unconfigured", last_attempt_at=None, last_synced_at=None)
    config = _object(paths.config_dir() / "dashboard.json")
    if config is None:
        return result
    url = config.get("page_url")
    if not isinstance(url, str) or not PAGE.fullmatch(url):
        raise ValueError("dashboard.json requires a canonical ChatGPT Space Page URL")
    result.update(page_url=url, status="never_synced")
    receipt = _object(store.home() / "dashboard-sync.json")
    if receipt is None or receipt.get("page_url") != url:
        return result
    state = receipt.get("status")
    if state not in ("synced", "failed"):
        raise ValueError("Dashboard sync receipt status must be synced or failed")
    attempt = _timestamp(receipt.get("last_attempt_at"))
    success = receipt.get("last_synced_at")
    if success is not None or state == "synced":
        success = _timestamp(success)
        if datetime.fromisoformat(success) > datetime.fromisoformat(attempt):
            raise ValueError("Dashboard sync receipt has a success newer than its last attempt")
    result.update(status=state, last_attempt_at=attempt, last_synced_at=success)
    return result


def render(result):
    if result["status"] == "unconfigured":
        return "Dashboard: not configured. Set page_url in dashboard.json under ORCHD_CONFIG_DIR."
    lines = [f"Dashboard: {result['page_url']}", f"Sync status: {result['status'].replace('_', ' ')}"]
    if result["last_attempt_at"]:
        lines.append(f"Last attempt: {result['last_attempt_at']}")
    if result["last_synced_at"]:
        lines.append(f"Last successful sync: {result['last_synced_at']}")
    return "\n".join(lines)
