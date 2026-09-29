# 終端前景／背景操作

使用者說「把 X 調到前景／打開 X」時，在目前終端接回指定的 native worker 或 Orch，不會另開 Ghostty 視窗。

## 操作

在 repo 根目錄執行：

```bash
./scripts/orch-foreground          # 在目前終端接回 Orch
./scripts/worker-foreground        # 只列出執行中的 native worker，輸入編號選擇
./scripts/worker-foreground my-app # 已知 ID 時直接接回
./scripts/list-agents              # 只列出存活的 Orch 與 workers，欄位對齊
```

選單顯示 ID、provider、backend、登記狀態與工作目錄；Enter、q 或 Ctrl-D 取消。
選單會核對 CLI 與 watcher 的 PID、啟動時間及停止旗標，不只依登記狀態判斷存活；tmux worker 不列入此 native 接回選單。
腳本只接回存活的 native Agent，不會啟動已停止的 Agent。tmux 請使用 `bin/agentctl worker attach ID`。
從其他目錄執行請用腳本絕對路徑。若管理組使用自訂 `AGENTCTL_HOME`，執行前須設成相同路徑；未設定時使用這份 checkout 的 `sandbox/state`。

`list-agents` 是唯讀清單，只列出存活的 Orch 與 workers：native CLI 與 watcher 必須都存活且不在停止中，tmux 則須確認 session 存在。欄位依內容寬度以空白對齊，顯示 ID、角色、provider、backend、最後登記的 state 與工作目錄；沒有存活 Agent 時顯示 `No running agents.`。清單不會啟動或修復任何 Agent。

按 **Ctrl+Z** 離開前景、回到 shell，背景 Agent 繼續工作。Claude 使用原生 detach；若 CLI 被 shell 暫停並顯示 suspended，使用 `fg` 回到原觀看程序。已 detach 時可重新執行上述腳本。

腳本以 exec 在目前終端執行既有 `worker attach`，沿用最新 session 定位與存活檢查，不自存 endpoint、不建立觀看視窗記錄，也不改變 manual 或 scheduler 狀態。沒有 TTY 時，既有 attach 只檢查存活並印出接回命令。

`ghostty-view.py` 檔名保留相容性，可直接執行 `python3 scripts/ghostty-view.py foreground WORKER_ID`；舊的 `background` 命令已移除，改在觀看終端按 Ctrl+Z。先前開啟的 Ghostty 視窗可自行關閉觀看連線。

明確「停止 X／停止整組」才使用 agentctl 的停止命令，不要用 `/exit` 代替離開前景。

## 驗證範圍

原生 CLI 的按鍵行為依版本而定；既有 Claude Code 2.1.263 驗證過 Ctrl+Z detach。這次修改驗證目前終端的 exec 路由與選單，未重新驗收真實 Codex／Claude TUI 的 Ctrl+Z 行為。

找不到資料庫或 worker 清單不符時，核對 `AGENTCTL_HOME`；沒有已登記的 worker 時，先依 [README](../README.md) 登記並啟動。
