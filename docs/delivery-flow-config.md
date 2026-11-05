# Worker delivery flow configuration

The optional `[delivery_flows]` table in orchd's `config.toml` maps product repo directory names to Hub repo directory names under `projects_dir`. It defaults to empty. Each Hub must be a local Git checkout with an `origin` and `origin/HEAD`; orchd fetches it and points workers to `docs/development/delivery.md` at the fetched full origin SHA. Missing checkouts, failed fetches, missing origin HEAD or missing entry are reported in the brief. The worker reads the pinned entry with the provided `git show` command.

```toml
[delivery_flows]
product-repo = "hub-repo"
```

The adapter is read from the `orchd` checkout under `projects_dir`, pinned to its current HEAD. Commit the adapter there before launching workers; an unavailable adapter is reported as missing.
