# orchd 指令參考

[English](commands.md)

以 `orchd/cli.py` 的 argparse 定義與 `orchd/goals.py` 的動態欄位為準。下列涵蓋所有指令、子指令、位置參數與旗標；每層都支援 `-h`／`--help`。

## 日常只需要記這幾個

| 指令 | 用途 |
| --- | --- |
| `orchd orch restart` | 重開目前 Desktop 綁定的 Orch，接手未結任務並重新 bind。 |
| `orchd list` | 查看未結任務與健康狀態。 |
| `orchd doctor` | 檢查機器就緒狀態。 |
| `orchd upgrade` | 更新 uv 安裝的程式與 managed 家目錄規則。 |

## BREAKING CHANGE：升級指令

Orch 指令現在必須明確使用 `orch start|stop|restart|list|attach`。舊入口已移除，不保留 alias；請依下表更新腳本、shell alias、操作手冊與交接文件。既有旗標和動作語意維持不變。

| 升級前（已移除） | 升級後 |
| --- | --- |
| `orchd orch [--model ...] [--no-attach]` | `orchd orch start [--model ...] [--no-attach]` |
| `orchd orch-stop ID` | `orchd orch stop ID` |
| `orchd orch-restart [ID]` | `orchd orch restart [ID]` |
| `orchd orchs` | `orchd orch list` |
| `orchd attach ID` | `orchd orch attach ID` |

呼叫已移除的入口會回傳 exit code 2 並提示新名稱。未指定子指令時會印出 Orch 子指令說明並回傳 2，不會啟動 Orch；`--help` 仍回傳 0。

## 安裝與維護

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd init [--from OLD_HOME] [--no-trust]` | 建立 home／interface 範本與 trust，保留已修改範本。 | `orchd init` |
| `orchd doctor [--profile example] [--json] [--no-input]` | 檢查機器是否就緒；終端機首次執行時可保存專案目錄。`--json`／`--no-input` 不詢問。 | `orchd doctor` |
| `orchd upgrade` | 從 uv 記錄來源更新程式並執行家目錄 migration；`--dry-run` 只預覽，`--migrations-only` 只重試 migration。 | `orchd upgrade` |

`init --from` 複製舊 Orch home 的內容；目標檔案若仍是未修改的 init 範本，會以來源檔案替換，其他既有檔案則保留並列出。略過 `.git`、`.gitignore`、`.DS_Store`、`.claude`、`.codex` 與 `AGENTS.md`；`--no-trust` 跳過 Codex trust 寫入與 Claude trust 檢查。`doctor` 結束碼：0 通過、1 必要檢查失敗、2 必要檢查未知；`--profile example` 是選用的合成範例。

## Orch 管理

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd orch start [--model sonnet\|opus] [--no-attach]` | 啟動 Claude Orch，預設直接 attach。 | `orchd orch start --model sonnet` |
| `orchd orch stop ORCH_ID` | 停止指定的 Claude Orch。 | `orchd orch stop ORCH_ID` |
| `orchd orch restart [OLD_ID] [--model sonnet\|opus] [--dry-run]` | 換新 Orch、接手未結任務，並重新綁定原有 Desktop 入口。 | `orchd orch restart` |
| `orchd orch list [--all] [--json] [--restore ORCH_ID]` | 列出存活／可恢復的 Orch；restore 清除封存並重設死亡觀察期。 | `orchd orch list` |
| `orchd orch attach ORCH_ID [--viewer]` | 連入既有 Orch，閒置時恢復對話。 | `orchd orch attach ORCH_ID` |
| `orchd adopt NEW_ORCH (TASK_ID ... [--from OLD_ORCH] \| --from OLD_ORCH) [--force]` | Operator：將未結任務移交給另一個 Orch。 | `orchd adopt NEW_ORCH --from OLD_ORCH` |

`orch start` 預設 model 為 opus；`--no-attach` 只啟動。`orch list` 預設隱藏死亡、未知與封存的 Orch；`--all` 顯示全部，`--json` 輸出完整 inventory。`orch attach --viewer` 開 Ghostty。`NEW_ORCH` 後必須至少給一個 `TASK_ID` 或 `--from OLD_ORCH`，也可兩者一起使用。`adopt --from` 可移交全部未結任務或限制指定 task ID；`--force` 也允許從存活或未知的 owner 移交並通知它。

## Desktop 入口

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd binding [--entry NAME] [--new \| --to ORCH_ID \| --status]` | 將 Desktop interface 綁定到存活的 Claude Orch，或查看綁定。 | `orchd binding` |

先跑 `orchd init` 建立 interface 設定。`binding` 預設 entry 為 desktop；尚無綁定時才會自動啟動 Orch，已有綁定則沿用。已綁定的 Orch 停止或確認死亡時會報錯，需用 `--new` 明確啟動並重新綁定，舊 Orch 的未結問題仍留在原處。Claude 因閒置退出的 session 可原地 resume；失敗時也需 `--new`。`--new` 開新 Orch、`--to` 指定存活 Orch、`--status` 唯讀查綁定／健康／問題／delivery，三者互斥。

## 查看任務與目標

| 指令與完整參數 | 說明 | 常用範例 |
| --- | --- | --- |
| `orchd list` | 查看未結任務、worker／Orch 健康與未讀通知失敗。 | `orchd list` |
| `orchd watch [--since HH:MM]` | 持續查看 Orch／worker 訊息時間軸。 | `orchd watch` |
| `orchd summary [--since HH:MM]` | 按 Orch 彙整 worker、model、問題、並行與 token。 | `orchd summary` |
| `orchd board --html PATH` | 輸出私人靜態 HTML 快照，不消耗 inbox。 | `orchd board --html /tmp/orchd-board.html` |
| `orchd snapshot --json [--schema-version 1|2] [--answered-limit N]` | 從私人 DB／WAL 副本讀取看板 JSON，不遷移或消耗 inbox。 | `orchd snapshot --json --answered-limit 100` |
| `orchd task-question answer --task ID --question-id ID --text-file PATH --idempotency-key KEY [--actor dashboard\|operator]` | 直接回答最新待答 worker 問題，持久化冪等，輸出 JSON 收據。 | `orchd task-question answer --task abc --question-id 42 --text-file /tmp/answer.txt --idempotency-key dashboard-42` |
| `orchd task-question dismiss --task ID --question-id ID --reason TEXT --idempotency-key KEY [--actor dashboard\|operator]` | 將過期決定 dismiss，保存稽核並通知 Orch inbox，不送 worker、不 close 任務。 | `orchd task-question dismiss --task abc --question-id 42 --reason expired --idempotency-key dismiss-42` |
| `orchd task link --task ID --ref REF [--actor orch\|operator] [--idempotency-key K]` | 設定主票參照，以新事件取代前一 link；輸出 JSON，不寫 tracker。 | `orchd task link --task example-task --ref jira:DEMO-7` |
| `orchd task unlink --task ID [--actor orch\|operator] [--idempotency-key K]` | 清除目前參照，保留歷史；不改 task／tracker 狀態。 | `orchd task unlink --task example-task` |
| `orchd goal add\|set\|show\|list\|export` | 透過下列五個子指令管理中央目標。 | `orchd goal list` |
| `orchd goal add REPO [GOAL_OPTIONS]` | 為 repo 建立目標並記錄稽核 actor。 | `orchd goal add example --type goal --intent "交付經審閱的修改"` |
| `orchd goal set GOAL_ID [GOAL_OPTIONS]` | 更新目標與稽核歷史。 | `orchd goal set GOAL_ID --status waiting --ball Operator` |
| `orchd goal show GOAL_ID` | 讀取目標、連結任務與完整歷史。 | `orchd goal show GOAL_ID` |
| `orchd goal list [--repo REPO] [--status active\|waiting\|paused\|done]` | 依 repo 或狀態讀取目標。 | `orchd goal list --repo example` |
| `orchd goal export --md [--repo REPO]` | 印出中央目標的 Markdown 快照。 | `orchd goal export --md` |

兩者的 `--since` 都使用今日本地 HH:MM。`watch` 從時間點之前最後一筆訊息的 ID 之後開始顯示，並持續追蹤新訊息；預設從最近 30 分鐘開始。`summary` 篩選該時間點（含）之後建立的任務，再彙整這些任務的所有訊息；預設包含全部任務。board 輸出檔案，重新執行才更新。

`snapshot` 必須帶 `--json`，包含 goals／history、未結 tasks、entry 問題／reply metadata，以及獨立的 worker 問題／送答證據。`--answered-limit` 必須非負，預設 100，只限制最近 answered entry 題；current／queued（含 held）不限制。舊 binding 與 closed task 關聯仍可見。worker 題標為待 Orch 分流，worker 確認／執行保持 unknown。輸出含 `generated_at`、`source_db`、輸出契約 `schema_version` 與實際 DB 的 `schema_fingerprint`。DB 不存在、損毀或 schema 不完整時會報錯並以非 0 exit code 結束。

### 主票參照：CLI、MCP 與 snapshot v2

以下以 main `9bfb35a` 的行為為準。REF 格式是 `github:owner/repo#N` 或
`jira:KEY-N`（N 正整數）；只驗證格式，不連外核對票存在或權限。
link／unlink 的 actor 預設 orch，僅作稽核歸屬。idempotency key 以 task
為範圍，省略時產生並回傳；相同 key 與 payload 重送回原 receipt，
即使後來已有新 link 也不恢復舊 ref；同 key 不同 ref／actor 拒絕。
JSON receipt 含 task_id、issue_ref、changed、actor、created_at、
message_id、supersedes、idempotency_key 與 status。失敗輸出 JSON，exit 1。

link 是 last-writer-wins 的 supersede，保留歷史而非拒絕覆蓋；沒有
expected-ref 旗標或舊值條件更新。refs 由 Orch 維護，換 ref 前先確認目前 ref。

MCP（不是 shell dispatch 指令）：

- `dispatch` 可選 `issue_ref`，省略即未掛票；其他必填參數維持原規則。
- `dispatch` 省略 `model` 時用設定的 worker 預設（出廠 `sonnet`）。worker 別名是層級階梯，由內建目錄加 `~/.config/orchd/config.toml` 的 `[models.*]` 決定（`orchd config show` 看有效值）：第 1 層 `terra`（GPT-6.1 Terra，`gpt-6.1-terra`，Codex）與 `sonnet`，`auto` 依剩餘額度二選一；第 2 層 `sol`；第 3 層 `fable`（Claude Fable 5.1）與 `astra`（`gpt-6-astra`）。`retry` 帶 `model="escalate"` 升一層（優先換另一家）；review 用同層另一家（terra↔sonnet、sol→sonnet、fable↔astra）。覆寫例：`[models.terra]` 加 `id = "gpt-6.2-terra"`，欄位 `id`、`vendor`（codex|claude）、`quota`（codex|claude，該別名耗用的額度桶；預設同 vendor）、`tier`；新程序讀檔即生效，不用改程式或重裝。`[interface]` 的 `model`／`effort` 決定 `orchd init` 寫進 Desktop interface `.codex/config.toml` 的值（出廠 `gpt-6.1-terra`、`low`）。quota 供應者沿用既有鍵：`sol` 是 Codex 桶（`limit_id` codex），`sonnet` 是 Claude 桶。Terra／Astra／Fable 在 `stats` 沒有價格（標未定價，不猜）。
  優先順序：dispatch 明確帶的 `model` > 環境變數 `ORCHD_WORKER_MODEL`（空字串視為未設）> `~/.config/orchd/config.toml` 的 `[worker] model` > `sonnet`。每一層都會驗證（即使上層會勝出），非法值一律報錯；只有省略 `model` 與空的環境變數視為未設，明確傳空字串、`0`、`false` 也報錯；設定檔是目錄、非法 TOML 或結構不對（如 `worker = "sol"`）也報錯並指出檔案。`ORCHD_CONFIG_FILE` 是測試／進階用的指定檔案 hook，優先於 `ORCHD_CONFIG_DIR`，`config show` 的 `file-source` 會顯示來源。
  用 `orchd config show|get|set|unset worker-model`（以及 `projects-dir`，與 doctor 第一次詢問專案目錄走同一個寫入器）查看與修改（show 會列出來源）；`unset` 即還原出廠預設。`retry` 仍必填 `model`；明確指定 `backend=app-server` 需要有效 model 為 `sol`。省略 backend 時，新的 `sol` 預設 app-server，`sonnet` 維持 Claude；明確指定 `backend=exec` 仍可使用 Codex exec。`config show` 顯示唯讀衍生欄位 `worker-backend`，來源 `model-default`，不是可 set/get 的設定鍵。既有 exec 不遷移，retry 沿用已存的 Codex backend（除非明確覆寫）。app-server 與 Claude 忙碌或閒置都可原生 attach；exec 只能在回合之間開啟。app-server 的 answer/followup FIFO 等所有 viewer 關閉後投遞。
- `task_link` 必填 `task_id`、`ref`，可選 `idempotency_key`。
- `task_unlink` 必填 `task_id`，可選 `idempotency_key`。

兩個 link 工具需 caller thread id，actor 固定 orch；沒有 Orch 對 task
的擁有權檢查（與 close、followup 一樣），Orch 必須自行核對 task。
dispatch 先 spawn 再寫 link；spawn 後 link 寫入失敗會報錯，但 task／worker
可能已在沒有 link 的狀態下執行。先用 list_open／snapshot 找到正確 task id
與目前 ref，再用 task_link／CLI link 補上，避免直接重派。

`orchd snapshot --json --schema-version 2` 在 top-level 與 embedded task
輸出 `issue_ref`（無參照為 null），另輸出
`issue_links: {version: 1, available: true, history: [...]}`。history 保存
link／unlink／superseded 的稽核事件；v1 不含這些新欄位。snapshot 只讀，
不遷移、不連 tracker；舊版缺欄位不代表已確認無票。

未升級的 MCP dispatch 沒有 issue_ref 時，在 instructions 記正式主票並
回報依賴未就緒，升級後核對再補正式 link。完整流程見
[先有票再派工](issue-linked-workflow.md)。

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

每個欄位旗標都需要值。一般欄位為字串；日期為 YYYY-MM-DD；`--companies` 為 JSON 字串陣列；`--linked-tasks` 為 JSON 陣列，元素含 `task_id` 與布林 `goal_critical`。`--v` 為 0–10 數字，記錄 Operator 核准值；`--j` 為 1、2、3、5、8。`--type` 為 goal／continuous，`--status` 為 active／waiting／paused／done。`--fields` 是 JSON object，欄位名稱用底線；旗標優先。清除可選日期或 v 必須透過 `--fields` 的 JSON null，例如 `orchd goal set GOAL_ID --fields '{"deadline": null, "v": null}'`；`--deadline null` 與 `--v null` 都無效，且不要再用同欄位旗標覆蓋 null。repo 建立後不可變。`--actor` 預設 `operator:<OS user>`，是稽核歸屬而非身分驗證。詳見[目標與看板](goals-board.md)。

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
orchd orch restart --dry-run
orchd orch restart
```

restart 已包含停舊 Orch、用新的 runtime／socket 查詢確認死亡、開新 Orch、adopt 未結任務，以及重新 bind 原本綁著舊 Orch 的 Desktop，**不用另外執行 binding**。daemon 與任務 worker 繼續執行。預設沿用舊 model；可用 `orchd orch restart OLD_ID --model sonnet` 明確指定。省略 OLD_ID 時，若 Desktop binding 不存在或過時，就列出候選並停止，讓你選擇。

成功後使用輸出的新 Orch ID，第一句提示為 `盤點上次`。任何步驟失敗或無法確認，都依輸出的補救指令處理，重試前核對任務 owner：adopt 可能已提交但通知失敗。若明確指定的 Orch 原本沒有綁 Desktop，Desktop binding 保持原狀，可用 `orchd orch attach NEW_ID` 對話。

### 換新電腦

1. 安裝[必要工具](../README.zh-TW.md#需求)，登入 Claude Code／Codex，在本機設定自己的 Git／SSH 存取。
2. 安裝公開程式並建立本機範本：

   ```sh
   uv tool install git+https://github.com/NatChung/orchd.git
   uv tool update-shell
   orchd init
   orchd doctor
   ```

3. 若已把舊 Orch home 複製到本機，可用 `orchd init --from OLD_HOME` 複製筆記；未修改的 init 範本會被替換，其他既有檔案保留。這只複製 home 內容，不移轉資料庫、執行中的 session 或 worker。個人路徑與帳號 mapping 留在本機，見[設定](../README.zh-TW.md#設定)；不要提交憑證或機器設定。
4. 在 `~/orch/home` 手動接受 Claude trust。執行 `orchd binding`，在 Codex Desktop 開啟 `~/orch/interface`，選 init 建立的 interface 權限，先要求 status，再交辦工作。用 `binding --status` 檢查入口。

### 升級

```sh
orchd upgrade
orchd doctor
```

upgrade 從 uv 記錄的來源安裝預設 branch 最新 commit，移除 branch pin，保留資料庫與個人設定。核對輸出的 commit；套件版本可能不變。既有 session、daemon 與 MCP process 不會重啟，可能仍使用舊程式。完成進行中的工作後，開始新 session／MCP process；換新 Orch 時使用上面的重開流程。

若安裝來源為 archive／舊來源，先從公開 HTTPS 來源重新安裝，見[遷移說明](../README.zh-TW.md#從私人-repo-遷移)。Checkout 安裝需在自己的 checkout 更新。

看板回答的 `entry-question answer`、`entry-question un-snooze`、生命週期欄位與
reply→worker 收據關聯，見[看板回答與 snapshot v2 說明](dashboard-answers.md)。
`answer` 固定輸出 JSON，沒有 `--json` 旗標。
`snapshot --json` 預設保留 v1 的欄位、值與列範圍；新增的生命週期與回答階段
需明確使用 `snapshot --json --schema-version 2`，也可用 `--schema-version 1` 選擇舊契約。

### Worker ask summary

Every worker `orchd ask ID "QUESTION"` must start with the following block.
Use plain language, about 5 lines and at most 300 characters; keep SHAs, paths,
logs, all required evidence and exact send previews after the standalone divider.
If there are no options, write `選項：無`. The impact line is optional.

```text
【給 Operator 的摘要】
要你決定：是否送出這份變更供審查？
選項：A 送出／B 暫緩
建議：選 A，測試已通過，可以交給 reviewer 檢查。
不回覆的影響：變更會保留在分支，等你決定。
---詳細---
完整送出預覽、測試輸出與證據放在這裡。
```

CLI `ask` warns on stderr without rejecting legacy bodies. MCP `ask_nat`
uses the same contract and returns an additive `warnings` list when the opening
block is missing. When quoting a worker question, put the Operator summary in
`text` and the full verbatim preview after `---詳細---`; quoting preserves the body.
For quote-only calls, the quoted worker body is checked instead of the quote prefix.
MCP warnings appear only in the result, after validation succeeds; they are not
also printed to stderr. Warnings do not mean the question was rejected or approved.

家目錄規則會在程式更新後自動以版本化步驟更新；managed 區塊外原文保留，覆寫前備份。旗標、自訂路徑與舊版首次更新步驟見[家目錄 migration](upgrade-migrations.md)。

## 目標的有序 issue 清單

`goal add/set --issues '["github:example/project#1","jira:DEMO-2"]'` 整份替換清單；
省略不改、`[]` 明確清空，拒絕 null 與重複 canonical ref。現有目標另帶
`--expected-revision N`，N 取 goal_list/show 的 `issue_scope.revision`；過期就重讀。
MCP 同用 `goal_set({goal_id, expected_revision, fields: {issues: [...]}})`。
Dashboard 用 `snapshot --json --schema-version 2`，區分未支援、尚未設定與已確認空清單。
詳見 [goal scope 契約](goals-board.md#ordered-issue-scope-refs-48)；部署、正式 DB 升級及重啟另行核准。
