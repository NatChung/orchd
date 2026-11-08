# 專案

`dispatch` 的 `repo` 是有效專案目錄底下的目錄名。有效專案目錄依序取 `ORCHD_PROJECTS`、個人設定 `config.toml` 的 `projects_dir`（個人設定目錄預設 `~/.config/orchd`，可用 `ORCHD_CONFIG_DIR` 覆寫）；都沒設定時，從 HOME 底下的 orchd checkout 執行就用其上層目錄，否則用預設 `~/projects`。worker 的 worktree 建在有效專案目錄的 `.orchd-worktrees/` 底下。

`orchd init` 建立本檔時的有效專案目錄：`${PROJECTS_DIR}`。之後改了設定，以 `orchd config show` 的 `projects-dir` 為準（由 Operator 在終端機執行）。

由 Operator 維護；Orch 只讀。

| repo | 說明 |
| --- | --- |
