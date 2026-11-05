# Worker delivery flow adapter

The task scope and orchd's worker rules govern all work. Follow the configured delivery flow as well when available; report missing guidance instead of assuming it does not apply.

Use only the pstack how, why, tdd and blast-radius methods where applicable. Read the relevant `SKILL.md` directly; do not invoke slash commands. This host's defaults are configurable, not guaranteed: Claude uses `~/.claude/skills/pstack-X/SKILL.md` and Codex uses `~/.agents/skills/pstack-X/SKILL.md`, where X is the method name. Report an unavailable skill as a gap.

Do the work yourself: do not start subagents, conduct cross-vendor reviews, send outward messages, or merge. Where a skill asks for those actions, do the applicable investigation yourself or report the gap. Follow orchd's preview and approval rules for outward actions.

- For how, ask who downstream uses each affected field and what happens to existing data.
- For why, distinguish evidence from inference and report evidence gaps.
- For tdd, use a failing behavior test first when practical.
- For blast-radius, verify the important safety facts with real code. For every identified risk, record whether you will change it or not, and why; ask before work outside scope.
- After selecting the base, run `codegraph init -y` or `codegraph sync` before code queries, according to the index state.
- Compare test results with a baseline from the same base; report failures and differences.
- If the diff touches locale or translation files, list new keys, changed existing values, who pushes to the translation platform (POEditor for web-next), whether non-English locales changed, and the risk of existing values being overwritten by the next pull in both the report and PR body. Do not edit non-English locales unless the ticket says so.
- For copy tickets, list every sibling state of each changed string (for example, Show more/Show less or Add to bag/Go to bag) and ask the PM via `orchd ask` before deciding.
- If a ticket attachment or required source is unavailable and the PM has not explicitly allowed a best guess, report blocked with evidence; do not make speculative commits.
- For UI tickets that change screens, styling, forms, or SSR output, verify the real page in Playwright/Chromium. Plan and exercise a width × state matrix appropriate to the change (for example phone, tablet, and desktop widths crossed with initial, error, success, and loading states). Record each width, state, action, and observed result in the report. Exercise form submission through its outcome, inspect the whole page for clipping rather than only an isolated component, and verify server-rendered output when affected.
- Keep browser evidence (screenshots, traces, and measurements) under `/tmp` or the job's temporary directory, outside the PR diff; identify its location in the report. If a width, state, submission, or server output cannot be verified (for example, no browser, login, or external service), explicitly list each unverified item and its reason in the report. Do not silently omit it. Pure backend, documentation, and configuration tickets do not require browser verification.
- For pages using SSR, ISR, or static generation, state whether server output is affected and verified; otherwise list it as unverified.
- Treat only expected not-found responses (for example, 404) as missing config; log and surface 5xx and network errors.
- Use PR title `type(TICKET-KEY): summary`, with a Conventional Commits type matching the runtime change.
- For cross-service work, the ticket must name the backend contract source and owner; otherwise mark the work provisional and do not report it complete.
