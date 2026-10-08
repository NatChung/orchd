# orchd 指令參考

[English](commands.md)

以 `orchd/cli.py` 的 argparse 定義與 `orchd/goals.py` 的動態欄位為準。下列涵蓋所有指令、子指令、位置參數與旗標；每層都支援 `-h`／`--help`。範例是操作指南，並非本次已執行的操作。

## 日常只需要記這幾個

| 指令 | 用途 |
| --- | --- |
| `orchd orch-restart` | 重開目前 Desktop 綁定的 Orch，接手未結任務並重新 bind。 |
| `orchd list` | 查看未結任務與健康狀態。 |
| `orchd doctor` | 檢查機器就緒狀態。 |
| `orchd upgrade` | 更新 uv 安裝的程式。 |

## 安裝與維護

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd init [--from OLD_HOME] [--no-trust]` | 建立 home／interface 範本與 trust，保留已修改範本。 | `orchd init` |
| `orchd doctor [--profile example] [--json]` | 唯讀檢查機器是否就緒。 | `orchd doctor` |
| `orchd upgrade` | 從 uv 記錄的來源更新程式。 | `orchd upgrade` |

`init --from` 複製舊 Orch home 保留的內容，不覆寫；`--no-trust` 跳過 Codex trust 寫入與 Claude trust 檢查。`doctor` 結束碼：0 通過、1 必要檢查失敗、2 必要檢查未知；`--profile example` 是選用的合成範例。

## Orch 管理

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd orch [--model sonnet\|opus] [--no-attach]` | 啟動 Claude Orch，預設直接 attach。 | `orchd orch --model sonnet` |
| `orchd orch-stop ORCH_ID` | 停止指定的 Claude Orch。 | `orchd orch-stop ORCH_ID` |
| `orchd orch-restart [OLD_ID] [--model sonnet\|opus] [--dry-run]` | 換新 Orch、接手未結任務，並重新綁定原有 Desktop 入口。 | `orchd orch-restart` |
| `orchd orchs [--all] [--json] [--restore ORCH_ID]` | 列出存活／可恢復的 Orch；restore 清除封存並重設死亡觀察期。 | `orchd orchs` |
| `orchd attach ORCH_ID [--viewer]` | 連入既有 Orch，閒置時恢復對話。 | `orchd attach ORCH_ID` |
| `orchd adopt NEW_ORCH [TASK_ID ...] [--from OLD_ORCH] [--force]` | Operator：將未結任務移交給另一個 Orch。 | `orchd adopt NEW_ORCH --from OLD_ORCH` |

`orch` 預設 model 為 opus；`--no-attach` 只啟動。`orchs` 預設隱藏死亡、未知與封存的 Orch；`--all` 顯示全部，`--json` 輸出完整 inventory。`attach --viewer` 開 Ghostty。`adopt --from` 可移交全部未結任務或限制指定 task ID；`--force` 也允許從存活或未知的 owner 移交並通知它。

## Desktop 入口

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd binding [--entry NAME] [--new \| --to ORCH_ID \| --status]` | 將 Desktop interface 綁定到存活的 Claude Orch，或查看綁定。 | `orchd binding` |

`binding` 預設 entry 為 desktop；沒有可用 Orch 時會啟動一個。`--new` 開新 Orch、`--to` 指定存活 Orch、`--status` 唯讀查綁定／健康／問題／delivery，三者互斥。

## 查看任務與目標

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd list` | 查看未結任務、worker／Orch 健康與未讀通知失敗。 | `orchd list` |
| `orchd watch [--since HH:MM]` | 持續查看 Orch／worker 訊息時間軸。 | `orchd watch` |
| `orchd summary [--since HH:MM]` | 按 Orch 彙整 worker、model、問題、並行與 token。 | `orchd summary` |
| `orchd board --html PATH` | 輸出私人靜態 HTML 快照，不消耗 inbox。 | `orchd board --html /tmp/orchd-board.html` |
| `orchd goal add\|set\|show\|list\|export` | 透過下列五個子指令管理中央目標。 | `orchd goal list` |
| `orchd goal add REPO [GOAL_OPTIONS]` | 為 repo 建立目標並記錄稽核 actor。 | `orchd goal add example --type goal --intent "交付經審閱的修改"` |
| `orchd goal set GOAL_ID [GOAL_OPTIONS]` | 更新目標與稽核歷史。 | `orchd goal set GOAL_ID --status waiting --ball Operator` |
| `orchd goal show GOAL_ID` | 讀取目標、連結任務與完整歷史。 | `orchd goal show GOAL_ID` |
| `orchd goal list [--repo REPO] [--status active\|waiting\|paused\|done]` | 依 repo 或狀態讀取目標。 | `orchd goal list --repo example` |
| `orchd goal export --md [--repo REPO]` | 印出中央目標的 Markdown 快照。 | `orchd goal export --md` |

`watch --since`／`summary --since` 使用今日本地 HH:MM；預設 watch 為最近 30 分鐘、summary 為全部。board 輸出檔案，重新執行才更新。

### GOAL_OPTIONS：add 與 set 的全部旗標

`--actor ACTOR`、`--fields JSON`，以及下列欄位旗標（兩個子指令皆支援）：

```text
--type --intent --pg --sprint-goal
--done-when --evidence --authority --status
--ball --blocker --source --plan
--sprint-start --sprint-end --follow-up-date --last-confirmed-date
--deadline --last-progress-date --waiting-nat-since --companies
--v --j --linked-tasks
```

每個欄位旗標都需要值。一般欄位為字串；日期為 YYYY-MM-DD；`--companies` 為 JSON 字串陣列；`--linked-tasks` 為 JSON 陣列，元素含 `task_id` 與布林 `goal_critical`。`--v` 為 0–10 數字，記錄 Operator 核准值；`--j` 為 1、2、3、5、8。`--type` 為 goal／continuous，`--status` 為 active／waiting／paused／done。`--fields` 是 JSON object，欄位名稱用底線；旗標優先，可用 JSON null 清除可選日期或 v。repo 建立後不可變。`--actor` 預設 `operator:<OS user>`，是稽核歸屬而非身分驗證。詳見[目標與看板](goals-board.md)。

## Worker 回報

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd ack TASK_ID` | 開始工作前確認收到任務。 | `orchd ack TASK_ID` |
| `orchd progress TASK_ID "TEXT"` | 送出中途進度，任務繼續執行。 | `orchd progress TASK_ID "測試通過，準備審閱"` |
| `orchd ask TASK_ID "QUESTION"` | 送出問題或完整操作預覽，結束本輪並等待回答。 | `orchd ask TASK_ID "請核准以下完整操作預覽：..."` |
| `orchd report TASK_ID --status done\|blocked --summary TEXT [--evidence TEXT]` | 提交最終結果與證據；done 仍待核對。 | `orchd report TASK_ID --status done --summary "文件已更新" --evidence "commit 與測試結果"` |
| `orchd verify TASK_ID [--timeout SECONDS]` | 驗證 worker：在鎖定 SHA 重跑鎖定的驗證指令。 | `orchd verify TASK_ID` |

文字有空白時用引號。report 的 evidence 預設空字串，但應提供 commit、測試與未驗證事項。verify 只用於已配置的驗證任務；timeout 單位為秒。對外動作先 ask 完整預覽並等待明確回答；report done 不代表部署或驗收。

## 內部與進階

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd mcp [--role orch\|entry] [--entry NAME]` | 啟動 Orch 或 Entry 工具的 stdio MCP server。 | `orchd mcp --role entry --entry desktop` |
| `orchd flush TASK_ID [--after-pid PID]` | Turn shell 整合：Codex 結束後送出排隊回答。 | `orchd flush TASK_ID --after-pid PID` |
| `orchd stats [--since TIME] [--json]` | 按 Orch／任務類型讀取有來源依據的任務、token 與成本估計。 | `orchd stats --json` |
| `orchd close TASK_ID` | 停止任務 worker，在安全條件下清理 worktree。 | `orchd close TASK_ID` |

`mcp` 預設 role 為 orch、entry 為 desktop；entry 名稱用於 entry role。`flush` 是 shell 整合，不是一般人工回報步驟。`stats --since` 接受今日本地 HH:MM 或含 Z／offset 的 ISO-8601，預設全部；成本為估計值。`close` 會停止 worker 並可能刪除 worktree，需依任務授權操作。

## 常見情境

### 重開 Desktop 綁定的 Orch

```sh
orchd orch-restart --dry-run
orchd orch-restart
```

restart 已包含停舊 Orch、用新的 runtime／socket 查詢確認死亡、開新 Orch、adopt 未結任務，以及重新 bind 原本綁著舊 Orch 的 Desktop，**不用另外執行 binding**。daemon 與任務 worker 繼續執行。預設沿用舊 model；可用 `orchd orch-restart OLD_ID --model sonnet` 明確指定。省略 OLD_ID 時，若 Desktop binding 不存在或過時，就列出候選並停止，讓你選擇。

成功後使用輸出的新 Orch ID，第一句提示為 `盤點上次`。任何步驟失敗或無法確認，都依輸出的補救指令處理，重試前核對任務 owner：adopt 可能已提交但通知失敗。若明確指定的 Orch 原本沒有綁 Desktop，Desktop binding 保持原狀，可用 `orchd attach NEW_ID` 對話。

### 換新電腦

1. 安裝[必要工具](../README.zh-TW.md#需求)，登入 Claude Code／Codex，在本機設定自己的 Git／SSH 存取。
2. 安裝公開程式並建立本機範本：

   ```sh
   uv tool install git+https://github.com/NatChung/orchd.git
   uv tool update-shell
   orchd init
   orchd doctor
   ```

3. 若已把舊 Orch home 複製到本機，可用 `orchd init --from OLD_HOME` 保留筆記而不覆寫。這只複製 home 內容，不移轉資料庫、執行中的 session 或 worker。個人路徑與帳號 mapping 留在本機，見[設定](../README.zh-TW.md#設定)；不要提交憑證或機器設定。
4. 在 `~/orch/home` 手動接受 Claude trust。執行 `orchd binding`，在 Codex Desktop 開啟 `~/orch/interface`，選 init 建立的 interface 權限，先要求 status，再交辦工作。用 `binding --status` 檢查入口。

### 升級

```sh
orchd upgrade
orchd doctor
```

upgrade 從 uv 記錄的來源安裝預設 branch 最新 commit，移除 branch pin，保留資料庫與個人設定。核對輸出的 commit；套件版本可能不變。既有 session、daemon 與 MCP process 不會重啟，可能仍使用舊程式。完成進行中的工作後，開始新 session／MCP process；換新 Orch 時使用上面的重開流程。

若安裝來源為 archive／舊來源，先從公開 HTTPS 來源重新安裝，見[遷移說明](../README.zh-TW.md#從私人-repo-遷移)。Checkout 安裝需在自己的 checkout 更新。

## CLI 涵蓋證據

2026-10-08 以本 checkout CLI 核對：24 個頂層指令、goal 的全部 5 個子指令，含根層共 30 次 help 呼叫皆成功。每次都只用 `--help`，在 dispatch 前結束，沒有執行實際應用操作。清單來自 argparse（包含 goal 動態欄位），並逐一核對兩個語言版本的全部旗標。可在具備專案依賴的 Python 環境重現：

```sh
python3 bin/orchd --help
for command in mcp orch orch-stop orch-restart init upgrade binding ack report progress flush ask verify list goal board orchs attach watch summary stats adopt close doctor; do
  python3 bin/orchd "$command" --help
done
for command in add set show list export; do
  python3 bin/orchd goal "$command" --help
done
```
