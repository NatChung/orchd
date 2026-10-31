# A close that will not finish

`orchd close TASK` leaves the task in its old status with the note `close pending, ...` when it cannot finish. Issue #96.

List them (read-only, changes nothing):

```bash
orchd stuck-close
```

Each row has the task, its worktree, the last error and the next step. Closing again is always safe.

## What close does now

- A worker pid that no longer exists counts as stopped. Only processes marked with this task's worktree are ever
  signalled; a reused pid or an unrelated process is left alone.
- If orchd cannot prove the worktree is safe to delete, it **keeps the worktree and still closes the task**. The
  task note says `worktree kept: <reason>`. Reasons include uncommitted changes, commits not pushed, a submodule
  with unsaved work or one git cannot read (`cannot verify submodule ...`), and git refusing the removal
  (`git worktree remove failed ...`). Nothing is ever removed with `--force`.
- A worktree with an initialized submodule is always kept; git can only remove it with `--force`.

## Still `close pending`

The worker could not be confirmed stopped (`claude agents` unavailable, a process alive after SIGKILL, a process
orchd cannot inspect). Check the cause, then run `orchd close TASK` again.

## Finishing by hand after "worktree kept"

```bash
git -C <worktree> status --porcelain                 # uncommitted changes
git -C <worktree> submodule foreach --recursive 'git status --porcelain; git stash list'
git -C <worktree> log --oneline @{u}..HEAD           # unpushed commits (also check inside each submodule)
```

Push or save what you need, then `git -C <repo> worktree remove --force <worktree>` yourself. Do this only after
you have looked: `--force` deletes a submodule's git dir unchecked.
