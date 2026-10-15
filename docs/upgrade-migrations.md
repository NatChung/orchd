# Upgrade 的家目錄規則更新

`orchd upgrade` 更新 uv 安裝的程式後，自動用**新安裝的 CLI** 執行尚未套用的 post-upgrade steps；程式已是最新也會執行。它不重啟 Orch、worker、daemon 或 Desktop。`--dry-run` 只預覽目前版本的 pending steps 和 diff，不重裝、不建備份、不寫檔或 migration state。

```sh
orchd upgrade --dry-run       # 選用：先看規則 diff
orchd upgrade                 # 日常唯一更新入口
orchd upgrade --migrations-only  # 只有 migration 失敗時重試，跳過程式安裝
```

較舊的已安裝版本沒有 post-upgrade runner：第一次用舊程式跑 upgrade 只能安裝新版，需再跑同一個指令才能執行 migration。從這種版本首次套用可直接跑 `orchd upgrade && orchd upgrade`；之後只跑一次 `orchd upgrade`。尚未合併到來源 branch 的 PR 不會被一般 upgrade 安裝。

## 範圍與保存

目前的 `orch-rules-v1` 只更新 `home/AGENTS.md` 與 `interface/AGENTS.md`。每份範本包含一個 `<!-- orchd:managed:start v=1 -->` … `<!-- orchd:managed:end -->` 區塊，規則由 orchd 維護；Operator 的自訂規則放在區塊外。區塊外的位元組（包含換行）保持原樣。

- 有一個完整 managed 區塊：只替換該區塊。
- 舊檔沒有區塊：保留完整原檔，在末尾附加 managed 區塊。摘要會提示「原檔已有的相同規則可以手動刪掉」。若舊規則與新規則衝突，Operator 應移除舊的重複／衝突段落；程式不猜測哪些原文可刪。
- 檔案不存在：以 init 的 AGENTS.md 範本建立。
- 不完整、順序錯誤或重複的標記、symlink 檔案：拒絕修改，修正後再重試。

目錄依明確的 `ORCHD_ORCH_HOME`／`ORCHD_INTERFACE_HOME` 或 `ORCHD_ROOT` 設定，否則使用 init 記錄的實際路徑，最後才用 `~/orch/home`、`~/orch/interface`。自訂安裝請沿用相同設定。init 將路徑寫入 `$ORCHD_HOME/home-migrations.sqlite3`（預設 `~/.local/share/orchd/home-migrations.sqlite3`）；舊安裝沒有紀錄時使用上述設定／預設。

此 migration 不更新 `.codex/config.toml`、PROJECTS、handoffs、groups、CLAUDE.md 或全域 trust。一般 init 仍只建立缺少的範本，保留內容不同的既有檔案；這次新增路徑紀錄，不自動刷新既有規則。

## 備份、失敗與還原

每次覆寫前，先把原文與權限備份到 orchd state 目錄（與 `home-migrations.sqlite3` 同一處，`ORCHD_HOME` 或 `~/.local/share/orchd`）下的 `backups/<role>/AGENTS.md.YYYYMMDD-HHMMSS-<唯一後綴>`（`<role>` 為 `home` 或 `interface`），不會在 Orch home 的 git repo 裡留下檔案。舊版本曾在原檔旁留下 `AGENTS.md.orchd-backup-*`，升級不會自動刪除，確認不需要後可手動刪掉。新檔沒有前一份可備份。摘要列出更新的檔案、備份與還原方式：把備份複製回摘要列出的原檔位置；若是新建檔，刪掉新檔即可。還原檔案不會撤銷已完成 step 的 DB 紀錄，因此同版本不會自動再次覆蓋還原內容。

step 以穩定 id、home/interface 路徑組合和套用時間記在上述 SQLite，不根據家目錄標記猜進度。各 step 依序執行，全部檔案處理成功才標記；失敗停在當前 step，已成功的前置 steps 保留。失敗前已更新的檔案和備份也保留，重試辨識區塊，不重複附加。併發執行透過 state DB 的寫入 transaction 序列化。

程式安裝成功後，migration 失敗不改變安裝成功的結果；摘要會顯示失敗並提供 `orchd upgrade --migrations-only`。單獨 migrations-only 失敗回傳非零結束碼。

## 後續版本

`orchd/home_migrations.py` 的 `STEPS` 是依序執行的 `(穩定 id, idempotent callable)` registry。修改 managed 規則時更新範本版本，並加入新的 step id；不要重用已套用的 id。新 callable 必須能處理先前 step 部分寫入後重試的情況。步驟紀錄不當作規則完整性檢查：同版本已標記後會跳過，區塊內的手動修改需自行還原或等下一個 migration。
