"""Channel layout provisioning; never loads credentials or contacts providers."""
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import tomllib
import uuid
from pathlib import Path

from . import paths, platform_process

VERSION = 1
TEMPLATES = Path(__file__).parent / "templates" / "channel"


def _locations(home, env):
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    state = Path(env.get("ORCHD_HOME") or home / ".local/share/orchd").absolute() / "migrations" / "channels"
    if not env.get("ORCHD_ROOT") and not env.get("ORCHD_CHANNEL_HOME"):
        from . import home_migrations
        _check_path(home_migrations.state_path(home, env))
        con = home_migrations.connect(home, env, readonly=True)
        try:
            if con:
                saved_home = home_migrations.targets(home, env, con)["home"]
                return (saved_home.parent / "channel").absolute(), state
        finally:
            if con:
                con.close()
    return paths.channel_home(home, env).absolute(), state


def _check_path(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError("path traversal is forbidden")
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if component.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError(f"refusing symlink or reparse point: {component}")



def _validate_file(file):
    _check_path(file)
    if os.name != "nt":
        info = file.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("channel file is not private")
        return
    # Windows files inherit the protected directory DACL; inspect owner and
    # every allowed ACE through the Windows security API, not chmod bits.
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    token = wintypes.HANDLE()
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    error = adv.GetNamedSecurityInfoW(str(file), 1, 0x5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise OSError(error, "cannot inspect channel file ACL")
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        needed = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        current = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not owner or not dacl or not adv.EqualSid(owner, current):
            raise ValueError("channel file is not private")
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        if not count:
            raise ValueError("channel file has no access rules")
        for index in range(count):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.c_ubyte.from_address(ace.value).value != 0 or not adv.EqualSid(ace.value + 8, current):
                raise ValueError("channel file is not private")
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.LocalFree(descriptor)

def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _files():
    return {str(p.relative_to(TEMPLATES)).replace("\\", "/"): p.read_bytes()
            for p in sorted(TEMPLATES.rglob("*.toml"))}


def _check_legacy(manifest):
    if manifest is None:
        return
    manifest = Path(manifest)
    _check_path(manifest)
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ValueError("unrecognized legacy manifest") from None

    def contains_secret(value):
        if isinstance(value, dict):
            for key, item in value.items():
                name = key.lower().replace("-", "_")
                if any(word in name for word in ("token", "password", "secret", "credential", "signing_key", "client_key")) and not name.endswith("_ref"):
                    return True
                if contains_secret(item):
                    return True
        return isinstance(value, list) and any(contains_secret(item) for item in value)

    if contains_secret(document):
        raise ValueError("secret_externalization_required")
    # This baseline has no recognized orchd-owned legacy channel manifest format.
    raise ValueError("unrecognized legacy manifest; manual conversion required")


def plan(*, home=None, env=None, legacy_manifest=None):
    _check_legacy(legacy_manifest)
    root, state = _locations(home, env)
    _check_path(root)
    _check_path(state)
    files = _files()
    for name, data in files.items():
        target = root / name
        _check_path(target)
        if target.exists():
            if not target.is_file():
                raise ValueError(f"channel layout conflict: {target}")
            _validate_file(target)
            if target.read_bytes() != data:
                raise ValueError(f"channel layout conflict: {target}")
    return {"root": str(root), "layout_version": VERSION, "files": sorted(files)}


def _mkdir_private(path):
    _check_path(path)
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"not a directory: {path}")
        platform_process.validate_private(path)
        return False
    path.mkdir(mode=0o700)
    platform_process.make_private(path)
    return True


def _atomic(path, data):
    fd, tmp = tempfile.mkstemp(prefix=".orchd-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _state(root, state):
    # Parents above the migration directory are managed by existing orchd state.
    state.parent.mkdir(parents=True, exist_ok=True)
    _mkdir_private(state)
    db_path = state / "layout.sqlite3"
    _check_path(db_path)
    db = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    if os.name != "nt":
        os.chmod(db_path, 0o600)
    _validate_file(db_path)
    db.execute("CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, root TEXT NOT NULL, status TEXT NOT NULL, files TEXT NOT NULL, dirs TEXT NOT NULL)")
    db.execute("BEGIN IMMEDIATE")
    return db


def apply(*, home=None, env=None, legacy_manifest=None):
    planned = plan(home=home, env=env, legacy_manifest=legacy_manifest)
    root, state = _locations(home, env)
    files = _files()
    state.parent.mkdir(parents=True, exist_ok=True)
    _mkdir_private(state)
    lock_path = state / "lock.sqlite3"
    _check_path(lock_path)
    lock = sqlite3.connect(lock_path, timeout=30, isolation_level=None)
    if os.name != "nt":
        os.chmod(lock_path, 0o600)
    _validate_file(lock_path)
    lock.execute("BEGIN IMMEDIATE")
    db = None
    try:
        db = _state(root, state)
        complete = db.execute("SELECT id FROM migrations WHERE root=? AND status='complete' ORDER BY rowid DESC LIMIT 1", (str(root),)).fetchone()
        if complete:
            if all((root / name).is_file() and _digest((root / name).read_bytes()) == _digest(data) for name, data in files.items()):
                return {**planned, "migration_id": complete[0]}
            raise ValueError("channel layout conflict: a migration-owned file is missing")
        row = db.execute("SELECT id, files, dirs FROM migrations WHERE root=? AND status='pending'", (str(root),)).fetchone()
        if row:
            migration, entries, dirs = row[0], json.loads(row[1]), json.loads(row[2])
            if {entry.get("path") for entry in entries} != set(files):
                raise ValueError("invalid channel migration journal")
            for entry in entries:
                name = entry["path"]
                if entry.get("new_hash") != _digest(files[name]) or entry.get("original") != str(root / name):
                    raise ValueError("invalid channel migration journal")
            if any(Path(name) != root and not Path(name).is_relative_to(root) for name in dirs):
                raise ValueError("invalid channel migration journal")
        else:
            migration = uuid.uuid4().hex
            entries = [{"path": name, "original": str(root / name), "old_hash": None, "new_hash": _digest(data), "backup": None, "created": not (root / name).exists(), "status": "pending"} for name, data in files.items()]
            dirs = []
            db.execute("INSERT INTO migrations VALUES (?, ?, 'pending', ?, ?)", (migration, str(root), json.dumps(entries), json.dumps(dirs)))
        db.commit()  # receipt exists before any channel file is written
        for directory in [root, *(root / n for n in ("email", "slack", "line")), *(root / n / "state" for n in ("email", "slack", "line"))]:
            if not directory.exists():
                directory.parent.mkdir(parents=True, exist_ok=True)
                if str(directory) not in dirs:
                    dirs.append(str(directory))
                    db.execute("UPDATE migrations SET dirs=? WHERE id=?", (json.dumps(dirs), migration))
                _mkdir_private(directory)
            else:
                _check_path(directory)
                platform_process.validate_private(directory)
        for entry in entries:
            target = root / entry["path"]
            _check_path(target)
            if target.exists():
                if _digest(target.read_bytes()) != entry["new_hash"]:
                    raise ValueError(f"channel layout conflict: {target}")
            else:
                _atomic(target, files[entry["path"]])
            _validate_file(target)
            if _digest(target.read_bytes()) != entry["new_hash"]:
                raise ValueError("channel layout verification failed")
            if entry["status"] != "complete":
                entry["status"] = "complete"
                db.execute("UPDATE migrations SET files=? WHERE id=?", (json.dumps(entries), migration))
        db.execute("UPDATE migrations SET status='complete' WHERE id=?", (migration,))
        return {**planned, "migration_id": migration}
    finally:
        if db is not None:
            db.close()
        lock.rollback()
        lock.close()


def rollback(migration_id, *, home=None, env=None, apply=False):
    root, state = _locations(home, env)
    _check_path(state)
    if not apply:
        return _rollback(migration_id, root=root, state=state, apply=False)
    lock = sqlite3.connect(state / "lock.sqlite3", timeout=30, isolation_level=None)
    try:
        lock.execute("BEGIN IMMEDIATE")
        return _rollback(migration_id, root=root, state=state, apply=True)
    finally:
        lock.rollback()
        lock.close()


def _rollback(migration_id, *, root, state, apply):
    _check_path(state)
    db = sqlite3.connect((state / "layout.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT root, files, dirs, status FROM migrations WHERE id=?", (migration_id,)).fetchone()
    finally:
        db.close()
    if not row or row[0] != str(root) or row[3] != "complete":
        raise ValueError("unknown migration")
    entries = json.loads(row[1])
    directories = json.loads(row[2])
    # Validate the entire receipt before making any changes.
    for entry in entries:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe migration receipt")
        _check_path(root / relative)
    for name in directories:
        directory = Path(name)
        if directory != root and not directory.is_relative_to(root):
            raise ValueError("unsafe migration receipt")
        _check_path(directory)
    conflicts = []
    removed = set()
    for entry in entries:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe migration receipt")
        target = root / relative
        _check_path(target)
        if not entry["created"] or not target.exists():
            continue
        if not target.is_file() or _digest(target.read_bytes()) != entry["new_hash"]:
            conflicts.append(str(target))
        else:
            if apply:
                target.unlink()
            removed.add(target)
    for name in reversed(directories):
        directory = Path(name)
        if directory != root and not directory.is_relative_to(root):
            raise ValueError("unsafe migration receipt")
        _check_path(directory)
        if directory.exists():
            if apply:
                try:
                    directory.rmdir()
                    removed.add(directory)
                except OSError:
                    conflicts.append(str(directory))
            elif any(child not in removed for child in directory.iterdir()):
                conflicts.append(str(directory))
            else:
                removed.add(directory)
    if apply and not conflicts:
        db = sqlite3.connect(state / "layout.sqlite3")
        try:
            db.execute("UPDATE migrations SET status='rolled_back' WHERE id=? AND status='complete'", (migration_id,))
            db.commit()
        finally:
            db.close()
    return {"migration_id": migration_id, "conflicts": conflicts}
