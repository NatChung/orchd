# Prompt to create and maintain your own workflow

Give the following prompt to an agent with access to your authorized source repositories. Replace the bracketed inputs. Use the same prompt for initial creation or a later method update. It describes requested work, not blanket permission to publish, install, start processes, or send messages.

```text
Help me create or maintain my own engineering method and Orch configuration using
orchd's existing interfaces. Read docs/user-owned-workflows.md in the public orchd
checkout, then follow its linked manifest, instance, run-template, and installation
documentation. Verify commands against the checked-out commit and local CLI help.
Do not invent flags or assume a particular provider, computer, OS, or global cache.

Inputs:
- Mode: [create | update | diagnose an existing installation]
- Actual OS and shell: [inspect locally, or ask if the target computer is unavailable]
- Public orchd checkout and reviewed installed commit: [paths and full SHA, or unknown]
- Method repository: [my example-pstack path and upstream basis, or create proposal]
- Workflow/configuration repository: [my example-orch path, or create proposal]
- Runtime root and projects directory: [explicit separate physical paths]
- Existing profile, owner, interface, and channel constraints: [preserve or unknown]
- Accepted task input and scope: [issue/specification and approval boundaries]
- Authorized changes and outward actions: [exact repositories and permitted operations]
- Required checks and done_when: [actual acceptance criteria]

First inspect the existing source and machine. Report what you observed, what you
inferred, and what remains unknown. Preserve user edits and existing instances.
If the target computer is unavailable, prepare a reviewable plan and machine
checklist. Do not claim that machine's setup, Windows behavior, login, or reboot
was tested. Ask only for missing decisions that affect authority or scope.

Keep the responsibilities separate:
- orchd supplies generic dispatch, instance application, run state, and native
  verification. Reuse its existing APIs.
- My method repository owns principles, playbooks, skills, tools, tests, and
  upstream provenance. Check the upstream license before deriving its content.
- My configuration repository owns committed workflow stages, fixed run templates,
  instance instructions, and project inventory. Do not put engineering scripts in
  a generated Orch home or add executable workflow hooks.
- Product repositories retain their own accepted inputs, guides, checks, and
  independent review. A workflow or method cannot grant authority.
- Channel adapters own transport credentials, exact route selection, and reply
  delivery. Workflow binding alone does not change channel routing.

For creation, propose the smallest repository layout and create a committed,
synthetic investigation example with orchd-workflow.json, referenced Markdown,
orchd-instance.json, and profile documents. Keep generated runtime state outside
tracked Git files. Use existing native instance check/apply commands. Check first.
Do not apply to a live profile or start an Orch until that action is authorized.

Define a project-owned method pin and resource verification contract. State that
orchd does not implement this contract for us. Fix a full Git commit and file
manifest digest. Give workers accessible resources and a trusted verifier command
with an external expected digest. Test relocation, wrong hashes, missing files,
unsafe paths, and the wrong target repository. Record actual method reads and
results without treating those declarations as authenticated acceptance.

For updates, inspect the current pin, upstream changes, retained failure evidence,
and all affected callers before editing. Keep old accepted inputs and evidence
immutable. Reproduce the defect, implement the smallest supported correction, run
native checks, and request a separate reviewer for the exact head. Do not mutate
an active method bundle, change the global skill cache, or adopt a candidate just
because its author says it passed.

Create reviewable task branches and PRs only where publication is authorized.
Follow each repository's issue and review conventions. Do not merge your own task's
PR. After independent review, propose a separate consumer pin update and controlled
check/apply/restart sequence. Keep installed and loaded-process versions distinct.

If multiple profiles share a background CLI, supply the complete explicit profile
environment to every MCP launch and supervisor. Verify the actual process and
interface path, not just inventory. Test exact channel routing with isolated probe
identities and local captures before any approved external send. Preserve existing
pending requests, credentials, owners, and production routes outside the scope.

Choose startup procedures for the actual OS. Do not copy macOS LaunchAgents into
Windows instructions. Test alive-binding idempotency before process recovery;
resume history rather than replacing the owner. Record changes to job/session IDs.
Treat controlled recovery, login, and real reboot as separate test results.

Return the changed paths and exact source commits; the runnable commands and raw
exits; independent review status; method adoption and instance receipts; actual
runtime checks; remaining gaps; and the next owner. Keep credentials, private names,
channel IDs, real home paths, and customer payloads out of public commits and logs.
Do not claim access to my computer, publication, deployment, or acceptance without
actual evidence.
```
