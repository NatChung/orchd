# Worker delivery flow configuration

The optional `[delivery_flows]` table in orchd's `config.toml` maps product repo directory names to Hub repo directory names under `projects_dir`. It defaults to empty. Each Hub must be a local Git checkout with an `origin` and `origin/HEAD`; orchd fetches it and points workers to `docs/development/delivery.md` at the fetched full origin SHA. Missing checkouts, failed fetches, missing origin HEAD or missing entry are reported in the brief. The worker reads the pinned entry with the provided `git show` command.

```toml
[delivery_flows]
product-repo = "hub-repo"
```

The adapter is read from the `orchd` checkout under `projects_dir`, fetched from origin and pinned to origin/HEAD, just like the Hub. A failed fetch or unavailable adapter is reported as missing; the local HEAD is never used. Successful and failed fetches are cached per process for five minutes. Each brief has a 30-second Git budget, with a 12-second maximum per Git command. Retry resolves SHAs again.
