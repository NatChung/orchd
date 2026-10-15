"""Versioned post-upgrade rules, tracked outside the user-editable Orch home.

Add a new step id when changing managed rules; never reuse an applied id.
Only AGENTS.md is managed here: permissions/config and Operator notes stay with init.
"""
import difflib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from . import bootstrap, paths

START = re.compile(rb"<!-- orchd:managed:start v=[0-9]+ -->")
END = b"<!-- orchd:managed:end -->"
SCHEMA = """
CREATE TABLE IF NOT EXISTS homes(role TEXT PRIMARY KEY, path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applied(step TEXT NOT NULL, scope TEXT NOT NULL,
 applied_at REAL NOT NULL, PRIMARY KEY(step, scope));
"""


def state_dir(home, env):
    return Path(env.get("ORCHD_HOME") or Path(home) / ".local/share/orchd").absolute()


def state_path(home, env):
    return state_dir(home, env) / "home-migrations.sqlite3"


def connect(home, env, *, readonly=False):
    file = state_path(home, env)
    if readonly:
        try:  # no state DB (fresh machine) means nothing was ever recorded: not an error
            return sqlite3.connect(file.as_uri() + "?mode=ro", uri=True) if file.exists() else None
        except sqlite3.OperationalError:
            if file.is_file():
                raise
            return None
    file.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(file, timeout=30)
    con.executescript(SCHEMA)
    return con


def record_init(home, env):
    con = connect(home, env)
    try:
        with con:
            for role, folder in (("home", paths.orch_home(home, env)),
                                 ("interface", paths.interface_home(home, env))):
                con.execute("INSERT OR REPLACE INTO homes VALUES(?, ?)", (role, str(folder.absolute())))
    finally:
        con.close()


def targets(home, env, con):
    saved = dict(con.execute("SELECT role, path FROM homes")) if con else {}
    return {role: Path(env.get(key) or (str(folder) if env.get("ORCHD_ROOT") else saved.get(role, str(folder))))
            for role, key, folder in (
                ("home", "ORCHD_ORCH_HOME", paths.orch_home(home, env)),
                ("interface", "ORCHD_INTERFACE_HOME", paths.interface_home(home, env)))}


def updated(original, template):
    """Replace one valid block or append on first adoption; preserve outside bytes."""
    starts = list(START.finditer(original))
    if b"orchd:managed:" not in original:
        return original + (b"\n\n" if original and not original.endswith(b"\n") else b"\n" if original else b"") + template, bool(original)
    if len(starts) != 1 or original.count(END) != 1 or original.count(b"orchd:managed:") != 2:
        raise ValueError("malformed or duplicate managed markers; repair the markers before retrying")
    start, end = starts[0].start(), original.index(END)
    if end < starts[0].end():
        raise ValueError("managed end precedes start; repair the markers before retrying")
    # The newline after the closing marker belongs to the user's outside content.
    block = template.rstrip(b"\n")
    return original[:start] + block + original[end + len(END):], False


def write_file(file, content, backup_dir):
    """Backup into backup_dir (outside the repo) before atomic replacement; preserve an existing file's permissions."""
    file.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if file.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=file.name + "." + time.strftime("%Y%m%d-%H%M%S") + "-", dir=backup_dir)
        os.close(fd)
        backup = Path(name)
        shutil.copy2(file, backup)
    fd, name = tempfile.mkstemp(prefix=".orchd-rules-", dir=file.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        if backup:
            shutil.copymode(backup, name)
        os.replace(name, file)
    except Exception as error:
        raise OSError(f"could not update {file}; backup: {backup or 'none (new file)'}; {error}") from error
    finally:
        Path(name).unlink(missing_ok=True)
    return backup


def update_rules(folders, dry_run, lines, backup_root):
    for role, folder in folders.items():
        file = folder / "AGENTS.md"
        if file.is_symlink():
            raise ValueError(f"refusing symlink: {file}")
        original = file.read_bytes() if file.exists() else b""
        template = (bootstrap.TEMPLATES / role / "AGENTS.md").read_bytes()
        content, appended = updated(original, template)
        if original == content:
            lines.append(f"unchanged: {file}")
            continue
        if appended:
            lines.append(f"{file}: appended managed rules; 原檔已有的相同規則可以手動刪掉。")
        if dry_run:
            lines.extend(difflib.unified_diff(original.decode().splitlines(), content.decode().splitlines(),
                                             fromfile=str(file), tofile=str(file) + " (managed)", lineterm=""))
        else:
            backup = write_file(file, content, backup_root / role)
            lines.append(f"updated: {file}; backup: {backup or 'none (new file)'}")
            lines.append(f"restore: copy {backup} over {file}" if backup else f"restore: remove newly created {file}")


# Append ordered (stable id, idempotent callable) steps for future releases.
STEPS = (("orch-rules-v1", update_rules), ("orch-rules-v2", update_rules))


def run(*, home=None, env=None, dry_run=False):
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    lines, con = [], None
    try:
        con = connect(home, env, readonly=dry_run)
        if con and not dry_run:
            con.execute("BEGIN IMMEDIATE")  # serialize concurrent upgrades across file writes
        folders = targets(home, env, con)
        scope = json.dumps({role: str(folder.absolute()) for role, folder in folders.items()}, sort_keys=True)
        for step, action in STEPS:
            if con and con.execute("SELECT 1 FROM applied WHERE step=? AND scope=?", (step, scope)).fetchone():
                lines.append(f"{step}: already applied")
                continue
            lines.append(f"{step}: {'preview' if dry_run else 'applying'}")
            action(folders, dry_run, lines, state_dir(home, env) / "backups")
            if not dry_run:
                con.execute("INSERT INTO applied VALUES(?, ?, ?)", (step, scope, time.time()))
                con.commit()
                con.execute("BEGIN IMMEDIATE")
        lines.append("Dry-run: no files, backups or migration state written." if dry_run else "Home migrations complete; restart sessions when ready to load updated rules.")
        return 0, lines
    except Exception as error:
        if con and not dry_run:
            con.rollback()
        return 1, lines + [f"Home migration failed (step not marked applied): {error}",
                           "Previously updated files/backups are retained; retry: orchd upgrade --migrations-only"]
    finally:
        if con:
            con.close()


def pending(home=None, env=None):
    """Step ids not yet applied to the recorded homes. Read-only; [] when orchd was never init'd/upgraded here."""
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    con = connect(home, env, readonly=True)
    if con is None:
        return []
    try:
        folders = targets(home, env, con)
        scope = json.dumps({role: str(folder.absolute()) for role, folder in folders.items()}, sort_keys=True)
        done = {row[0] for row in con.execute("SELECT step FROM applied WHERE scope=?", (scope,))}
    finally:
        con.close()
    return [step for step, _ in STEPS if step not in done]


def ensure_current(*, home=None, env=None, write=True):
    """Startup check (CLI and MCP server): apply stale home rules, or warn. Returns lines for stderr; never raises.

    Applying goes through run(), so it backs up first, keeps bytes outside the managed block, and is serialized
    by BEGIN IMMEDIATE. ORCHD_AUTO_MIGRATE=0 (or write=False) only warns.
    """
    env = os.environ if env is None else env
    fix = "orchd upgrade --migrations-only"
    try:
        todo = pending(home, env)
        if not todo:
            return []
        if not write or env.get("ORCHD_AUTO_MIGRATE") == "0":
            return [f"orchd: Orch home rules are behind this version ({', '.join(todo)}); run `{fix}`."]
        code, lines = run(home=home, env=env)
        if code:
            return [f"orchd: Orch home rules are behind this version ({', '.join(todo)}) and auto-update failed; run `{fix}`.",
                    *lines[-2:]]
        applied = [line for line in lines if line.startswith(("updated:", "restore:"))]
        return [f"orchd: updated stale Orch home rules ({', '.join(todo)}); restart Orch sessions to load them.", *applied]
    except Exception as error:
        return [f"orchd: could not check Orch home rules ({error}); run `{fix}`."]
