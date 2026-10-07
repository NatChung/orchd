# Interrupted workers and their worktrees

What to do when `list_open` shows `worker_health: "orphan"`: the task is `running` or `acked`, its worker is
confirmed dead (the Claude daemon lists the job as `failed` or no longer lists it; a Codex turn exited), and it never
sent `report`. See issue #17.

orchd only labels the task. It does not change the task's status, stop anything, retry, adopt, or remove a
worktree. Every step below is manual, and the Orch decides what follows.

## What the labels mean

| `worker_health` | Meaning | Action |
|---|---|---|
| `alive` | The worker process or job is running. | None. |
| `unknown` | Liveness could not be confirmed: `claude agents` failed, or a Codex thread is between turns (`worker_alive: null`). | Not a death. Do not guess from timestamps; check again later. |
| `finished` | The task is `done`/`blocked` or the worker sent a report. A gone worker is a normal exit. | Read the report as usual. |
| `orphan` | `running`/`acked`, worker confirmed dead, no report. | Follow this runbook. |
| `null` | Status outside this check (`starting`, `failed`, `question`). | — |

An orphan label says the worker is gone, not why. A daemon restart, a reap, or a crash all look the same here.

## 1. Inspect the worktree before deciding anything

The worktree and branch are in the `list_open` row and in `recovery_hint`. Read-only checks:

```bash
git -C <worktree> status --porcelain             # uncommitted changes
git -C <worktree> log --oneline <base>..HEAD     # commits the worker made
git -C <worktree> ls-remote origin refs/heads/<branch>   # what is pushed
```

Then put the worktree in one of these groups.

**Clean**: no uncommitted changes, and either no new commits or every commit is on origin. Nothing local would be
lost. The branch (if pushed) is where a rework continues from.

**Dirty**: uncommitted changes. They exist only in this directory. Treat them as the dead worker's unfinished
work, not as garbage: a half-done edit, a deletion in progress, or files a worker staged and never committed all
look the same. Do not run `git checkout .`, `git clean`, `git stash`, or `git worktree remove --force` on it.

**Unpushed**: commits that origin does not have. The local branch is the only copy. Keep it.

**Missing**: the worktree directory is gone (`git worktree list` may still show it as prunable). Whatever was not
pushed is lost; check the branch on origin and the task's messages for what the worker had reached.

**Interrupted in the middle of a destructive step**: many tracked deletions or a partly removed tree (seen once on
a `chasr` worktree, cause not verified) means something stopped halfway. Record what `git status` shows, keep the
directory, and tell Operator before anyone touches it.

## 2. Decide how to continue

The Orch decides, or asks Operator:

- **Rework**: dispatch a new task with `rework_of=<orphan task id>`. In its instructions, name the old branch and
  worktree, say what the inspection found, and tell the worker to start from that branch (pushed commits) or to
  read the dirty worktree before redoing the work. The new task gets its own worktree; the old one stays untouched.
- **Ask Operator** when the worktree is dirty, unpushed, or interrupted mid-deletion, or when the task touched anything
  outward (email, Slack, deploys) and it is unclear what was already sent.

Same-worker continuation and model escalation on the same worktree are issue #5 (followup / retry), not part of
this runbook.

## 3. Closing the orphan and removing its worktree

`orchd close` keeps any worktree that is dirty or has unpushed commits and records why in the task's note; it only
removes a worktree when nothing local would be lost. Close the orphan only after the rework (or Operator) has taken what
it needs.

Removing a kept worktree by hand (`git worktree remove`, `--force`, `rm -rf`, deleting the branch) destroys the
only copy of that work. It needs Operator's explicit confirmation for that specific worktree, every time. orchd does not
do it, and an Orch or worker must not do it on its own, including batch cleanup of old worktrees under
`~/projects/.orchd-worktrees/`.
