"""Look up the Dashboard binding and run actions through its local Pages publisher."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

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


def page_url():
    config = _object(paths.config_dir() / "dashboard.json")
    if config is None:
        return None
    url = config.get("page_url")
    if not isinstance(url, str) or not PAGE.fullmatch(url):
        raise ValueError("dashboard.json requires a canonical ChatGPT Space Page URL")
    return url


def status():
    result = dict(page_url=None, status="unconfigured", last_attempt_at=None, last_synced_at=None)
    url = page_url()
    if url is None:
        return result
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


def run_action(action):
    url = page_url()
    if url is None:
        raise ValueError("Dashboard Page is not configured")
    config_dir = paths.config_dir().resolve()
    config_path = config_dir / "pages-publisher" / "config.json"
    config = _object(config_path)
    if config is None:
        raise ValueError("Dashboard Pages publisher is not configured for this profile")
    node = config.get("node", "node")
    if not isinstance(node, str) or not node:
        raise ValueError("Pages publisher node must be an executable name or path")
    bridge = Path(__file__).with_name("dashboard_pages.cjs")
    try:
        process = subprocess.run(
            [node, str(bridge), action, url, str(config_path), str(store.home().resolve()), str(config_dir)],
            capture_output=True, text=True, timeout=55, check=False,
        )
    except FileNotFoundError as error:
        raise ValueError("Dashboard Pages publisher requires Node.js") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("Dashboard Pages publisher timed out; check Dashboard status before retrying") from error
    try:
        result = json.loads(process.stdout)
    except ValueError as error:
        raise ValueError("Dashboard Pages publisher returned no result") from error
    if not isinstance(result, dict):
        raise ValueError("Dashboard Pages publisher returned an invalid result")
    if process.returncode:
        raise ValueError(result.get("error") or "Dashboard Pages publisher failed")
    result["page_url"] = url
    if action == "refresh":
        outcome = result.get("status")
        receipt = status()
        if (outcome not in ("updated", "unchanged") or receipt["status"] != "synced"
                or receipt["page_url"] != url):
            raise ValueError("Dashboard refresh was not confirmed")
        result.update(receipt, status=outcome)
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
