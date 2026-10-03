# Orch file permissions

`Runtime.start_orch` launches Claude with `--restricted`, `dontAsk`, and a strict
orchd MCP configuration. Its session-only CLI settings disable background
worktree isolation and allow edits only under the resolved Orch home's
`groups/**`. Claude's `Edit(path)` rule covers both Edit and Write; `Write(path)`
rules are not consulted by file tools. No unrestricted Edit or Write is allowed.

The only additional working directory is the temp tree for the Orch home's cwd:
`/private/tmp/claude-<uid>/<cwd-slug>/` on macOS (`/tmp` resolves to `/private/tmp`).
The uid comes from `os.getuid()`; the slug replaces each non-ASCII-alphanumeric
character of the resolved home path with `-`. The directory is created if missing
and set to mode 0700. It contains **all Orch home sessions' temp files, read-only**,
including images and scratch files. It is not the entire per-uid temp root.
Claude stores input images under `<cwd-slug>/<session-uuid>/images/<number>.png`
(other image formats retain their own extensions). Original dragged file paths
outside these working directories remain inaccessible; use the cached copy.

This applies only to Orch sessions launched by `start_orch`. Manually launched
background Orch sessions keep their own settings. Running sessions must be
recreated to acquire these launch settings. Neither the Orch home's settings nor
`~/.claude/settings.json` is edited. This cache layout was verified against Claude
Code 2.1.288 and is version-dependent. Homes with cwd keys longer than 200
characters fail closed rather than grant access to an incorrectly computed key.

## Mechanism and sources

- [Settings precedence](https://code.claude.com/docs/en/settings): managed > CLI
  `--settings` > project local > project > user. Lists normally merge.
  Claude 2.1.288's `--help` specifies that restricted mode ignores user, project,
  and local files; managed and CLI settings still apply.
- [Background isolation](https://code.claude.com/docs/en/agent-view#how-file-edits-are-isolated):
  `worktree.bgIsolation` accepts `worktree` or `none`, with no path-specific
  switch. The background guard rejected writes in the shared checkout before
  the write occurred. The generated CLI settings set `none` for this session.
- [File rules](https://code.claude.com/docs/en/permissions#read-and-edit): Edit
  rules control writes, and absolute rules use `//`. Restricted mode also checks
  working-directory membership before reads; a bare Read allow alone does not
  extend it. `additionalDirectories` opens only the home-specific temp tree.
- Local 2.1.288 image-store implementation and actual files confirmed the temp
  layout above. `--bg --session-id` did not retain the requested UUID, so this
  implementation uses one normal background launch with the home-specific tree.

## Scratch verification (2026-10-03)

Unmodified `start_orch` launch flow with its generated settings, isolated git
scratch home, and Claude Code 2.1.288 / Sonnet 5.5:
session `cd05f8c5-cd2e-40b9-adc3-453c130c41ed`, job `cd05f8c5`.

| Tool attempt | Actual result |
| --- | --- |
| Write `home/groups/probe38b.md` | Created successfully; disk content `GROUP_OK` |
| Write `home/outside.md` | Write denied in don't ask mode; file absent |
| Write `<cwd-slug>/outside.md` | Write denied in don't ask mode; file absent |
| Read `<cwd-slug>/<session>/images/test.png` | Tool returned an image block with `image/png` |

Tool records were saved without image bytes in
`/tmp/orchd-38-evidence/results.json`. Scratch session was stopped and removed;
scratch home, image/cache tree, and sockets were removed. Hash comparison of
the pre-probe Orch home files (excluding git metadata) and global
`~/.claude/settings.json` found no changes. Scratch trust initialization added
the `/private/tmp/orchd-38-x2t762kl/home` workspace key in `~/.claude.json`;
that single key was subsequently removed with Nat's authorization. All other
JSON keys and values were compared and remained identical. Cleanup hashes:
before `8cb214413d19150e0c75be2e7e097ec131f8feec7e936937c4558b98c1fc492b`,
after `ab470b9c1475ccc55d8b6f342bb60784f1d1d197297bed72a545f64a731ef6f2`.
