"""Private, bounded copies of Entry local_image inputs in Claude's permitted temp tree.

SQLite retains the Operator's original text. The manifest is disposable: if the OS
removes it, inbox can still report missing copies using the original rollout paths.
All filesystem operations below use directory descriptors rather than following
user-controlled source/destination symlinks. No scheduler or permission is added.
"""
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

from . import paths

MAX_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 10
TTL = 7 * 24 * 60 * 60
CLEAN_INTERVAL = 60 * 60
MANIFEST_LIMIT = 64 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
SLOT = re.compile(r"[0-9]{2}\.[a-z0-9]{1,10}")
COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _parts(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe_path")
    # macOS system aliases are fixed, trusted roots, not attachment symlinks.
    # Do not resolve the remainder: that would follow user-controlled links.
    if sys.platform == "darwin" and len(path.parts) > 1 and path.parts[1] in ("var", "tmp"):
        alias = path.parts[1]
        if Path("/" + alias).is_symlink() and os.readlink("/" + alias).lstrip("/") == "private/" + alias:
            path = Path("/private", *path.parts[1:])
    return path.parts[1:]


def _walk(path):
    fd = os.open("/", DIR_FLAGS)
    try:
        for name in _parts(path):
            next_fd = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _child(parent, name, create=False, private=True):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    s = os.fstat(fd)
    if s.st_uid != os.getuid() or s.st_mode & (0o077 if private else 0o022):
        os.close(fd)
        raise ValueError("unsafe_cache_directory")
    return fd


def _private_file(parent, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_nlink != 1:
        os.close(fd)
        raise ValueError("unsafe_cache_file")
    return fd


@contextlib.contextmanager
def _cache(create=False):
    image_root = paths.orch_images()
    fd = _walk(image_root.parent.parent)
    try:
        for name, private in ((image_root.parent.name, False), (image_root.name, False), ("attachments", True)):
            next_fd = _child(fd, name, create, private)
            os.close(fd)
            fd = next_fd
        lock = _private_file(fd, ".lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield fd, image_root / "attachments"
        finally:
            os.close(lock)
    finally:
        os.close(fd)


def _entry_name(entry_id):
    return entry_id if COMPONENT.fullmatch(entry_id) else hashlib.sha256(entry_id.encode()).hexdigest()


@contextlib.contextmanager
def _message(root, entry_id, message_id, create=False):
    if not isinstance(message_id, int) or message_id < 1:
        raise ValueError("unsafe_message_id")
    entry_fd = _child(root, _entry_name(entry_id), create)
    try:
        fd = _child(entry_fd, str(message_id), create)
        try:
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(entry_fd)


def _read_manifest(fd):
    with os.fdopen(_private_file(fd, "manifest.json", os.O_RDONLY), "rb") as stream:
        raw = stream.read(MANIFEST_LIMIT + 1)
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError("invalid_manifest")
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("created_at"), (int, float))
            or not math.isfinite(data["created_at"]) or data["created_at"] < 0
            or not isinstance(data.get("attachments"), list) or len(data["attachments"]) > MAX_IMAGES
            or not isinstance(data.get("omitted"), int) or data["omitted"] < 0):
        raise ValueError("invalid_manifest")
    for item in data["attachments"]:
        if (not isinstance(item, dict) or not isinstance(item.get("source_path"), str)
                or item.get("status") not in ("copied", "failed")
                or not isinstance(item.get("file"), str) or not SLOT.fullmatch(item["file"])
                or item.get("error") not in (None, "source_missing", "unsafe_source", "too_large", "copy_failed")
                or (item["status"] == "failed" and item.get("error") is None)):
            raise ValueError("invalid_manifest")
    return data


def _atomic_json(fd, data):
    raw = json.dumps(data, ensure_ascii=False).encode()
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError("manifest_too_large")
    name = ".copy-" + uuid.uuid4().hex
    try:
        with os.fdopen(_private_file(fd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as out:
            out.write(raw)
        os.rename(name, "manifest.json", src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass


def _copy(fd, source, name):
    parent = _walk(Path(source).parent)
    temporary = ".copy-" + uuid.uuid4().hex
    try:
        source_fd = os.open(Path(source).name, FILE_FLAGS, dir_fd=parent)
        try:
            s = os.fstat(source_fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid():
                raise ValueError("unsafe_source")
            if s.st_size > MAX_BYTES:
                raise ValueError("too_large")
            with os.fdopen(source_fd, "rb", closefd=False) as inp:
                with os.fdopen(_private_file(fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as out:
                    size = 0
                    while chunk := inp.read(min(64 * 1024, MAX_BYTES + 1 - size)):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("too_large")
                        out.write(chunk)
        finally:
            os.close(source_fd)
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(parent)
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _cleanup(root, now):
    marker = _private_file(root, ".last_cleanup", os.O_RDWR | os.O_CREAT)
    try:
        if os.fstat(marker).st_size and now - os.fstat(marker).st_mtime < CLEAN_INTERVAL:
            return
        for entry_name in os.listdir(root):
            if not COMPONENT.fullmatch(entry_name):
                continue
            try:
                entry_fd = _child(root, entry_name)
            except (OSError, ValueError):
                continue
            try:
                for message in os.listdir(entry_fd):
                    if not re.fullmatch(r"[1-9][0-9]*", message):
                        continue
                    try:
                        fd = _child(entry_fd, message)
                    except (OSError, ValueError):
                        continue
                    try:
                        try:
                            created = _read_manifest(fd)["created_at"]
                        except (OSError, ValueError):
                            created = os.fstat(fd).st_mtime  # abandoned partial copies
                        if now - created < TTL:
                            continue
                        for name in os.listdir(fd):
                            if name == "manifest.json" or SLOT.fullmatch(name) or re.fullmatch(r"\.copy-[a-f0-9]{32}", name):
                                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                                if stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid():
                                    os.unlink(name, dir_fd=fd)
                    finally:
                        os.close(fd)
                    try:
                        os.rmdir(message, dir_fd=entry_fd)
                    except OSError:
                        pass
            finally:
                os.close(entry_fd)
            try:
                os.rmdir(entry_name, dir_fd=root)
            except OSError:
                pass
        os.write(marker, b"1")
        os.utime(marker, (now, now))
    finally:
        os.close(marker)


def cleanup():
    """Best effort, activity-triggered cleanup; never touch Claude's own image dirs."""
    try:
        with _cache() as (root, _):
            _cleanup(root, time.time())
    except (OSError, ValueError):
        pass


def capture(entry_id, message_id, sources):
    """Called under relay's SQLite write transaction, before any notification."""
    try:
        with _cache(create=True) as (root, _):
            _cleanup(root, time.time())
            with _message(root, entry_id, message_id, create=True) as fd:
                try:
                    _read_manifest(fd)
                    return None  # already captured; never recopy a changed/deleted source
                except FileNotFoundError:
                    pass
                items = []
                for i, source in enumerate(sources[:MAX_IMAGES], 1):
                    suffix = Path(source).suffix.lower().lstrip(".")
                    name = f"{i:02d}." + (suffix if re.fullmatch(r"[a-z0-9]{1,10}", suffix) else "image")
                    item = dict(source_path=source, file=name, status="copied")
                    try:
                        _copy(fd, source, name)
                    except FileNotFoundError:
                        item.update(status="failed", error="source_missing")
                    except ValueError as error:
                        item.update(status="failed", error="too_large" if str(error) == "too_large" else "unsafe_source")
                    except OSError:
                        item.update(status="failed", error="copy_failed")
                    items.append(item)
                _atomic_json(fd, dict(version=1, created_at=time.time(), attachments=items,
                                      omitted=max(0, len(sources) - MAX_IMAGES)))
    except (OSError, ValueError):
        return "cache_unavailable"
    return None


def describe(entry_id, message_id, sources):
    """No source copying here: missing/expired cache requires Operator to reattach."""
    items, omitted = [], max(0, len(sources) - MAX_IMAGES)
    try:
        with _cache() as (root, base), _message(root, entry_id, message_id) as fd:
            data = _read_manifest(fd)
            omitted = data["omitted"]
            for saved in data["attachments"]:
                item = dict(source_path=saved["source_path"], status=saved["status"])
                if saved["status"] == "failed":
                    item["error"] = saved["error"]
                elif time.time() - data["created_at"] >= TTL:
                    item.update(status="unavailable", error="expired")
                else:
                    try:
                        copy = _private_file(fd, saved["file"], os.O_RDONLY)
                        os.close(copy)
                        item["cached_path"] = str(base / _entry_name(entry_id) / str(message_id) / saved["file"])
                    except (OSError, ValueError):
                        item.update(status="unavailable", error="cache_missing_or_unsafe")
                items.append(item)
    except (OSError, ValueError):
        items = [dict(source_path=source, status="unavailable", error="cache_missing_or_unsafe")
                 for source in sources[:MAX_IMAGES]]
    return items, omitted


def present(data, entry_id, sources):
    items, omitted = describe(entry_id, data["message_id"], sources)
    if not items and not omitted:
        return
    data.update(source_body_bytes=data["body_bytes"], source_body_sha256=data["body_sha256"], attachments=items)
    lines = ["\n\n[orchd attachment copies — temporary, retained up to 7 days; reattach if unavailable]"]
    for item in items:
        lines.append("Original: " + json.dumps(item["source_path"], ensure_ascii=False))
        lines.append("Orch-readable copy: " + json.dumps(item["cached_path"], ensure_ascii=False)
                     if "cached_path" in item else "Copy unavailable: " + item["error"] + "; please reattach.")
    if omitted:
        lines.append(f"{omitted} additional attachments not copied (limit {MAX_IMAGES}); please send separately.")
    data["body"] += "\n".join(lines)
    raw = data["body"].encode()
    data.update(body_bytes=len(raw), body_sha256=hashlib.sha256(raw).hexdigest())


def mentioned_images(body):
    """Only a missing-cache hint from Desktop's wrapper; NEVER a copy source."""
    if "# Files mentioned by the user:" not in body:
        return []
    return re.findall(r"^## [^\n]+: (/[^\n]+)\nImage attachment: true\s*$", body, re.M)
