"""Validate the bound Page and maintain the last sync receipt for one orchd profile."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import time

from orchd.file_lock import try_lock


def read_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def write_atomic(path, data):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def update(state, config, page_id, action):
    if not state.is_absolute() or not config.is_absolute() or not (state / "orchd.db").is_file():
        raise ValueError("Provide absolute profile directories and an existing orchd.db")
    if not re.fullmatch(r"page_[A-Za-z0-9_-]+", page_id):
        raise ValueError("Expected a ChatGPT Space Page ID")
    url = f"https://chatgpt.com/space/{page_id}"
    if read_json(config / "dashboard.json").get("page_url") != url:
        raise ValueError("Configured dashboard Page does not match the publisher target")
    if action == "check":
        return

    path = state / "dashboard-sync.json"
    with path.with_suffix(".lock").open("ab") as lock:
        deadline = time.monotonic() + 3
        while True:
            try:
                try_lock(lock)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Dashboard receipt is busy")
                time.sleep(0.05)
        update_receipt(path, url, action)


def update_receipt(path, url, action):
    try:
        previous = read_json(path)
    except FileNotFoundError:
        previous = {}
    if previous.get("page_url") != url:
        previous = {}
    success = previous.get("last_synced_at")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if action == "synced" or (action == "verified" and not success):
        success = now
    write_atomic(path, {
        "page_url": url, "status": "failed" if action == "failed" else "synced",
        "last_attempt_at": now, "last_synced_at": success,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--page", required=True)
    parser.add_argument("action", choices=("check", "synced", "failed", "verified"))
    args = parser.parse_args()
    try:
        update(args.state, args.config, args.page, args.action)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Dashboard receipt: {error}\n")


if __name__ == "__main__":
    main()
