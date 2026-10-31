# A close that will not finish

`orchd close TASK` leaves the task in its old status with the note `close pending, ...` when it cannot finish. Issue #96.

List them (read-only, changes nothing):

```bash
orchd stuck-close
```

Each row has the task, its worktree, the last error and the next step. Closing again is always safe.

## What close does now

- A worker pid that no longer exists counts as stopped. Only "no such process" counts as gone: a pid orchd may not
  inspect (permission denied, unreadable environment) is *unknown*, and unknown blocks the close (`close pending`).
- A Codex turn is signalled only when orchd can prove it is this task's: the pid, birth time, group and worktree
  recorded when orchd started the turn must match, or (older turns) the process must carry this task's `ORCHD_WORKTREE` mark and
  lead its own group. A command line that merely mentions `codex` and a path proves nothing. Only the verified turn
  and its descendants are signalled, never a whole process group blindly.
- If orchd cannot prove the worktree is safe to delete, it **keeps the worktree and still closes the task**. The
  task note says `worktree kept: <reason>`. Reasons include uncommitted changes, **ignored files** (`.env`, local
  config, build output: they are in no commit and `git worktree remove` deletes them silently), commits not pushed,
  a submodule with unsaved work or one git cannot read (`cannot verify submodule ...`), and git refusing the
  removal (`git worktree remove failed ...`). Nothing is ever removed with `--force`.
- Ignored directories that are rebuilt from tracked files do not keep a worktree: exactly `.venv`, `node_modules`,
  `__pycache__`, `.pytest_cache`, `.mypy_cache` and `.ruff_cache`. Any other ignored path keeps it.
- A worktree with an initialized submodule is always kept; git can only remove it with `--force`.
- `orchd stuck-close` reads a private snapshot of the database and committed WAL without opening the source in SQLite. If there is no database, or it is older than this orchd, it says
  so and exits 1 without creating or migrating anything; run another orchd command (for example `orchd list`) to
  migrate, then retry.

## Still `close pending`

The worker could not be confirmed stopped (`claude agents` unavailable, a process alive after SIGKILL, a process
orchd cannot inspect). Check the cause, then run `orchd close TASK` again.

### Older (legacy) Codex turns

A record from before worktree ownership was stored is not enough to authorize a signal. If the process has this
worktree's `ORCHD_WORKTREE` mark and leads its own group, retry close normally. If its environment is unreadable,
close stays pending; do not delete the record or substitute the currently running process's birth time.

Inspect the task's saved PID with `ps -p <pid> -o pid=,ppid=,pgid=,lstart=,command=` and independently establish
which task started it (the task log and original launch environment). A command mentioning Codex or the worktree
is not proof. If it is another task's process, leave it alone. If ownership remains uncertain, leave close pending
and let the original turn finish or ask its operator to stop it through the original session. Only after ownership
is confirmed, stop that individual turn through its original session; then retry `orchd close TASK`. Do not use a
process-group kill or run worktree removal while any owned or unknown process remains.

## Finishing by hand after "worktree kept"

`git worktree remove --force` deletes everything in the worktree, ignored files included, and a submodule's whole git
dir with its stashes, local branches, tags and reflog. Nothing below is optional. Run it all first; stop and save
anything the checks list.

```bash
WT=<worktree>; REPO=<repo>
# 1. Working tree: uncommitted, untracked AND ignored files (.env, local config, build output)
git -C $WT status --porcelain --ignored=matching --untracked-files=all --ignore-submodules=none
# 2. Commits in no remote: every local branch and tag, not only the checked-out one.
#    A detached HEAD and a branch with no upstream both show up here; a clean `@{u}..HEAD` does not cover them.
git -C $WT branch -vv                                  # "gone" or no [origin/...] means no upstream
git -C $WT rev-list --branches --tags HEAD --not --remotes   # any output = commits that exist only here
git -C $WT stash list                                  # stashes are shared with the main repo
# 3. The same, inside every initialized submodule (including nested ones)
git -C $WT submodule foreach --recursive '
  pwd; git status --porcelain --ignored=matching --untracked-files=all
  git branch -vv; git tag --contains HEAD; git stash list
  git rev-list --branches --tags HEAD --not --remotes'
# 4. Reflog-only work: commits reachable only from a reflog (after a reset, rebase or branch delete)
git -C $WT reflog show --all --date=iso
git -C $WT submodule foreach --recursive 'pwd; git reflog show --all --date=iso'
```

For every commit that step 2 or 3 prints, push it (`git push origin <branch>`) or give it a branch name you keep. For
every reflog entry that is not already on a pushed branch, create a branch at that commit
(`git branch rescue/<name> <sha>`) and push it. Copy ignored files you still need out of the worktree.

Before the force, keep a copy of the submodule git dirs and the worktree's reflog. The submodule git dir lives under
the main repo's `.git/modules/` (and `.git/worktrees/<name>` for the worktree itself); removal deletes it:

```bash
BACKUP=~/orchd-close-backup-<task>; mkdir -p $BACKUP
export BACKUP
mkdir -p "$BACKUP/submodules"
git -C "$WT" submodule foreach --recursive '
  target="$BACKUP/submodules/$displaypath.gitdir"
  mkdir -p "$(dirname "$target")"
  cp -R "$(git rev-parse --absolute-git-dir)" "$target"'
cp -R "$(git -C $WT rev-parse --absolute-git-dir)" $BACKUP/worktree.gitdir   # holds the worktree's HEAD and reflog
```

Only then:

```bash
git -C $REPO worktree remove --force $WT
```

Keep `$BACKUP` until the work is confirmed saved.
