# agentctl 通訊協定（原型）

狀態：本機實作，2026-09-11 更新使用者自備 connector 介面；驗證範圍見 [next-steps.md](next-steps.md)。本文件給 Orchestrator（Claude Code／Codex TUI）與 worker 讀；日期較早的驗證段落保留歷史證據。

協調目標、worker 持續問答或查看派工紀錄時，讀 [目標協調與本地測試](goal-coordination.md)。`question new/answer` 與舊的終結式 `report question` 不同；目標整體驗證不以子任務回報成功代替。

## 管理模式

預設管理模式是 native；整組 Orch/workers 選定同一模式。完整操作與生命週期見 [native.md](native.md)。native 由原生背景 job／App Server 承載，觀看連線離開不視為停止整組，`agentctl stop` 才明確停止整組。native project hooks 不接任 Orch；身份、忙閒與訊息由原生觀察者和明確 ack/report 管理。

native／tmux 收件流程為 queued → sending → sent → acked → done/closed-*。sending 是傳輸 I/O 前持久化的保留狀態；未知結果轉 unconfirmed，需明確查證，不允許下一個 dispatch 越過未解決訊息。worker 先 `ack <id>` 再工作；Orch 收到 wake 後執行 `inbox --mark-read`，不 report wake。舊 tmux 流程依下列既有 hook 規則。

持久 task queue 先通過相依、manual、pause、容量與資源檢查，按有效優先級與各專案最後成功認領序號選擇；必要前置工作繼承下游 urgent 優先級。report done 只進 review，必須明確驗收才解除任務相依。逐任務 armed recovery 只處理已 ack 的 native Codex current attempt 及可信耗盡；停止已觀察程序後仍須人工確認 detached writer／外部結果，才交接給 Claude，不可把停止通知當成完成。

對外操作另有 gateway operation 紀錄，和 worker dispatch／ack/report 分開。經 gateway 呼叫的使用者 adapter 需精確 preview、digest、binding revision 與既有發訊授權；sending／unknown 不重放。email／Slack／LINE 預設停用，由使用者依 [connector 契約](connectors.md) 自行接入；離線 adapter 測試不代表真實服務驗收。完整命令與邊界見 [下方操作說明](#專案登記與外部操作)。

## 角色

- **使用者**：在 Orchestrator 的 TUI 打字交辦、回答決策。
- **Orchestrator**：由 agentctl 啟動並登記的協調者，預設 native；只透過 `agentctl` 派工、收件、提問。專案工作交給 cwd 對應的 worker。
- **Worker**：由整組選定的 native 或 tmux 承載的 Claude／Codex session，收到訊息後執行並明確 `report`。

native 啟動時指定專用 socket、登記 provider 回傳的 session/job ID；定位與傳輸見 [native.md](native.md#socket-與-session-id-定位)。tmux 的 hook 接任與生命週期見下方專屬章節。

每次執行 `agentctl` 都要帶 `AGENTCTL_HOME`（state 目錄）。路徑寫在 hook 設定與訊息檔裡；sandbox 用：

    export AGENTCTL_HOME=/absolute/path/to/agentctl/sandbox/state
    AGENTCTL=/absolute/path/to/agentctl/bin/agentctl

## 訊息狀態（分開記錄，不可混用）

| 狀態 | 意義 | 證據來源 |
|---|---|---|
| queued | 已存進 DB，尚未送 | `agentctl send` |
| sending | native／tmux 已持久保留，正在執行傳輸 I/O | agentctl 自己 |
| sent | 傳輸已返回成功；native 用 queue／UDS，tmux 用 send-keys | agentctl 自己；不等於收件 |
| acked | 已取得收件證據 | native 明確 ack 或 Codex userMessage 事件；tmux UserPromptSubmit hook |
| done / closed-blocked / closed-question | worker 明確回報 | `agentctl report`（唯一的完成證據） |
| unconfirmed | sent 後 60 秒沒 ack；**不自動重送** | pump 逾時判定 |
| interrupted | 停止或重新啟動後未完成派工的結果需查證，**不自動重送** | 管理生命週期／tmux hook |

`agentctl resend <msg-id>` 把 unconfirmed / interrupted 的訊息重新排隊，這是人（或 orchestrator 查證後）的明確決定。

tmux worker 的 Stop hook 只代表「這一輪講完」，不代表任務完成；Stop 時若有 acked 但未 report 的訊息會記進 events。

## tmux 專屬：Orch 啟動與退出

- 每份 clone 執行 `bin/agentctl install --shell`，在本機安裝兩種 CLI 的 hook，並備份、更新 `~/.zshrc`。新的 terminal 自動生效；已開 terminal 先 `source ~/.zshrc`。CLI 的 help／version 等管理命令不接任 Orch。
- 在 checkout 根目錄明確執行 `agentctl orch` 會先選管理模式（Enter＝native；此章需選 tmux），再選 CLI：Enter＝Claude skip、`2`＝Codex skip、`q`＝取消；指定 `--kind` 就不出 CLI 選單。普通 `claude`／`codex` 不啟動或接回管理。已有存活 Orch 時不出選單，直接 attach。非互動（沒有 tty）且沒給 `--kind` 一律報錯，不會停著等輸入。
- skip 預設：Claude `--dangerously-skip-permissions`、Codex `--dangerously-bypass-approvals-and-sandbox`；呼叫端自己帶了權限相關參數就不再疊加。受管 worker 的預設啟動命令同樣帶 skip，`worker start --command` 可覆寫。skip 只管權限提示，hook／MCP 信任是另一層，照樣要在 TUI 內確認。
- 已有非 tmux 的 Orch 在跑時，`orch` 預設拒絕啟動第二個；`--takeover` 明確接管（新的 tmux Orch claim 之後，舊 CLI 再 `/exit` 不會清掉 worker）。tmux session 名稱被占用但不屬於目前 Orch 時只報錯並給指令，不會替使用者砍掉活著的 session。
- Orch 跑在 tmux 裡，`agentctl orch` 只負責 attach。**detach 不等於退出**：tmux client 離開，CLI 還在，受管 worker 不會被清掉。要結束 Orch 走 CLI 的 `/exit` 或 `tmux -L agentctl kill-session -t orch`，兩者都讓 CLI 程序結束，監測程序才做清理。
- 「是不是 tmux-hosted」不存狀態，每次用 pane pid 與 owner pid（含祖先鏈）現場比對。只有明確 `orch --takeover` 能接管舊管理者；普通 terminal CLI 的 hook 不能接管。受管 worker（`AGENTCTL_WORKER`）與 Orch 自己的 tmux session（`AGENTCTL_ORCH_SESSION`）都不會再觸發選單或遞迴啟動。
- Codex 的 SessionStart 到第一個 prompt 才觸發，因此 `orch` 的內部 launcher 先登記 `launch-…` 身分；真正的 session_id 到 hook 時，必須符合已登記的 PID 與程序啟動時間才能接上。`launch` 僅限已登記的 Orch pane，普通 CLI 或殘留本機 hooks 無法自行建立或接管 Orch。
- 每個接管代號有獨立背景監測，檢查 CLI PID 與程序啟動時間；PID 被重用也不當成原 CLI。正常退出由 SessionEnd 要求清理，強制關閉由每秒存活檢查發現。Stop、等待使用者或模型閒置都不算退出。
- 只有目前接管代號可以清理。A 開啟 → B 接任 → A 退出，worker 繼續；B 退出，才結束同一 AGENTCTL_HOME 下 role=worker 的 tmux sessions。監測在接管代號失效或完成清理後退出；不碰未註冊的 tmux sessions。
- 清理與接管持有 SQLite 寫入鎖，避免舊 Orch 清理到新 Orch 的 worker。清理會清空 Orch／worker session_id，state 設 stopped；失敗會記 cleanup_retry 並重試。
- sent／acked 與未送出的 dispatch 訊息標 interrupted，保留回覆、決策和工作檔案；不自動重送。重新啟動 worker 後查證，再明確 resend。已接管走的舊 CLI 即使 compact 或晚到的第一個 prompt，也不能搶回 Orch。

## 派工規則

`quota`／`watch` 提供獨立觀察，不直接派工。新持久 scheduler 讀取觀察作保守 provider 選擇；預設停用，先 `scheduler configure --slots N` 再明確 `scheduler resume` 才允許 pump 認領。fresh／stale／unverified／unknown／reset-pending 不等於 worker busy／idle 或任務完成。Claude 目前帳號與 upstream 新鮮度未驗證，不能作可信自動 fallback；完整規則與命令見 [README](../README.md)。

- 一個 worker 同時只有一則未完成派工；native／tmux 的 sending 或 unconfirmed 也會阻擋後續派工。後續訊息排在 agentctl 的 queue，需前一則已回報且 worker idle 才送；native 由觀察者 pump，tmux 由 hook 等流程 pump。原因：實測 Codex 在忙碌中收到新訊息會插進當前 turn 並把原任務帶偏；Claude 會先回新訊息。
- `--urgent` 只是插到 queue 最前面，仍然等 idle 才送。
- worker 被切成 manual（`agentctl worker manual <id> on`）後不再自動派送，但原生觀察者／tmux hook 仍持續更新它的 state；`off` 時恢復並立即 pump。
- 訊息本文放在 `$AGENTCTL_HOME/msgs/<msg-id>.md`，經原生傳輸送入或由 tmux 打進 TUI 的只有一行 envelope：`[agentctl msg:<id>] ... read <path> ... When finished run: agentctl report <id> ...`。

## tmux 專屬：回報通知（wake）

worker `report`（以及使用者的 `answer`）把回覆放進 Orch inbox 之後，agentctl 會主動喚醒 tmux Orch，不用等使用者下一則 prompt：

- 通知是一則 `kind=wake` 的訊息，收件人是 Orch。內容一行：`[agentctl wake:<id>] N unread inbox item(s) ... inbox --mark-read`。它**不是派工**，`agentctl report <wake-id>` 會被擋掉。
- 只有 Orch idle、不在等權限、composer 空著的時候才打字；否則排隊。Stop／SessionStart 會安排 pump，Orch 程序監測也每秒重新檢查待送 wake，因此輸入框稍後清空不必等下一個模型 hook。已送出的 wake 不重送。
- 受管 tmux Claude Orch 啟動時設定 `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false`，避免 CLI 的自動建議被純文字畫面檢查當成草稿。只設定該程序環境，不修改全域 Claude 設定；真正的手動草稿仍阻擋通知。
- 同時只會有一則未結案的 wake：忙碌期間進來的多筆回報合併成同一則，送出時才數 inbox 有幾筆未讀。
- 狀態分開記：`wake.queued` → `wake.sent`（send-keys 完成）→ `wake.acked`（Orch 的 UserPromptSubmit 看到 tag）。逾時只標 `wake.unconfirmed`，不自動重送。
- Orch 送出任何 prompt 都會注入並清空 inbox，所以那一刻待送／已送的 wake 全部作廢（`wake.closed`），不會有已讀完還再被叫一次的情況。Orch 結束時也一併作廢。
- Orch 不在 tmux（或已被別的 CLI 接管）時記 `wake.fallback`，回到原本路徑：下一次 prompt 由 hook 注入 inbox，或自己跑 `inbox`。

## Orchestrator 常用指令

`orchestrator` 是邏輯收件地址，與目前登記的 Orch ID（通常是 `orch`）共用同一收件匣。預設 `inbox --mark-read` 或 `inbox --for orch --mark-read` 都讀取兩種地址的回報並確認 native wake；使用 `--from orch` 派工也能自動收到回報。


    $AGENTCTL status                 # worker / task / 未結案訊息
    $AGENTCTL inbox --mark-read      # 未讀回覆與提問（native 必須明確讀取；tmux prompt hook 也會注入）
    $AGENTCTL task new "<title>" --project <p>
    $AGENTCTL send <worker> --task <task-id> "<指令全文>"      # 或 body 用 - 從 stdin 讀
    $AGENTCTL ask "<需要使用者決定的問題>" --task <task-id>       # task 進 waiting_decision
    $AGENTCTL answer <q-id> "<使用者的回答>"                       # 只恢復該 task；回答進你的 inbox
    $AGENTCTL worker screen <id>     # 看 worker 畫面（診斷用，不是收件證據）
    $AGENTCTL log -n 40

規則：
- 不要輪詢等回覆。native 由原生訊息喚醒，收到後跑 `inbox --mark-read`；tmux 裡，worker 回報會被 agentctl 打成一行 wake 通知送進來；此外使用者下一次對你說話時 hook 也會以 additionalContext 塞進 inbox。要主動查就跑一次 `inbox`。
- 需要使用者決策的 task 用 `ask` 之後就停在那，去做其他已授權工作；使用者沒回答 = 繼續等，沉默不是同意。
- 收到 `[done]` 回覆代表 worker 自稱完成，task 進 `review`；Orch 依 goal 的有效授權與查證標準核驗；managed automatic 可在完整 pass 後完成，human 仍需指定驗收人。合併／部署遵守另行確認的執行範圍，report done 本身不提供授權。

## Worker 規則（訊息檔尾端也會重述）

- native 收到派工先 `agentctl ack <id>`，再讀 `msgs/<id>.md` 照做；tmux 收件由 hook 確認。做完**只跑一次** `agentctl report <id> --status done "<一行摘要>"`。
- 做不下去：`--status blocked`；需要人決定：`--status question`，摘要寫清楚問題。
- 不要自己去改 agentctl 的 state 或其他 worker。

## tmux 歷史驗證 / 待驗證

native 的真實 CLI 驗證與目前完整測試結果見 [native.md](native.md#驗證)。下列保留 tmux 各階段紀錄。

- 已驗證（2026-09-08 sandbox）：Claude 與 Codex 的 SessionStart / UserPromptSubmit / Stop hook 都會帶 session_id。早期測試曾觀察到 tmux 環境繼承，但 後續真實 Codex worker hook 實際未帶 `TMUX`，不能依賴它辨識 worker；Codex 的專案 hook 需要在 TUI 內信任（依 hook 內容 hash，指令字串改了就要重信任）；Codex 文字後立刻 Enter 會被當貼上而留在輸入框，需間隔約 1 秒；Codex 的 SessionStart 到第一個 prompt 才觸發。
- worker 的 hook 以實際程序祖先鏈比對註冊 socket／session 的 pane PID，不依賴 hook 的 `TMUX` 環境變數。同 cwd 的外部 CLI、同 socket 的其他 session 都會被忽略（`hook.ignored_foreign_session`）。Orchestrator hook 另以已登記 owner 的 PID 與程序啟動時間驗證；不再因 cwd 相同自動接任（2026-09-09 更新）。
- 已驗證（2026-09-08，隔離 AGENTCTL_HOME + 私有 tmux socket + 假 CLI，共 32 個測試）：idle 自動通知、busy 延後到 Stop、多回報合併成一則、prompt ack 後可再次通知、非 tmux Orch 走 inbox fallback、被 terminal CLI 接管後不再對舊 tmux 打字、composer 未就緒時排隊、wake 不能被 report、Orch 結束清掉待送 wake、選單預設與非互動拒絕、兩種 CLI 的 skip 預設與 `--command` 覆寫、tmux 內 launch claim 到的是 CLI 本身的 pid。
- 未驗證：真的用 Claude／Codex TUI（而非假 CLI）跑完整 wake 循環、60 秒無 ack 的 `unconfirmed` 逾時路徑、長時間背景執行、手機 Remote Control、多個 Claude session 共用同一 cwd 時 hook 的辨識（worker id 仍來自啟動環境或 hook command 的預設值，再以 pane 祖先鏈驗證；同 cwd 多 worker 的環境若全被清除，預設 id 可能不符，仍需驗證）。

## 持久排程任務與 execution attempts

`scheduler enqueue` 保存可等待的任務，`send` 保存指向特定接收方的訊息。scheduler 的原子認領在 SQLite 交易內同時寫入 attempt、worker／cwd／具名資源 claim 與 dispatch；提交後才走既有傳輸。排程未啟用、paused、worker manual、Orch 不在、容量不足或未知送達都不繞過原本派工前提。`scheduler plan` 無模型呼叫，也不執行派送。

attempt 綁定 task、worker、session generation、session ID 與 cwd；task 的 current attempt 決定哪一輪可更新它。晚到 report 保存證據，但不覆寫新的任務輪次。訊息 report、attempt 結束與 task 驗收是不同狀態；scheduler task 需 `task set TASK done` 明確驗收才解除 prerequisite。總任務需子任務先驗收，不能用 inbox 已讀、ack 或 worker 的 done 回覆代替。

report 後資源仍保留，直到同 generation／session 有新 idle 觀察。若程序失去、停止或結果未知，不能因時間到了自動再派。先查證 worker 與衍生寫入者停止，`scheduler resolve ATTEMPT --evidence ...` 保存停止證據，之後才能 `scheduler retry TASK --provider ...`；resolve 的程序檢查不提供完整 OS 隔離保證。重試產生新 attempt，不修改原 dispatch 收件人。

native／tmux 都在外部 I/O 前持久寫入 sending。傳輸失敗或中途退出後，未知結果保留為阻塞證據；需要查證與明確 resend／resolve，不能自動切 queue／UDS／tmux 通道。早到 ack／report 不得被傳輸成功後的 sent 寫回蓋掉。

每個 task／blocker 最多保存兩種失敗 method；第二種後阻擋後續派工及驗收，需 `scheduler resolve-blocker` 保存解除證據。資料跨 retry／重啟保留。terminal alerts 持久保存，`scheduler status`／`watch` 查看、`scheduler ack-alert ID` 確認讀取。確認 alert 不等於解除 scheduler pause。額度重置也不自動 resume。

跨 provider retry 需要已停止並 resolve 的 current attempt，以及該 attempt 的 ready handoff package；`--provider auto` 也不能繞過此條件。`handoff prepare` 原子保留 task／worker／cwd，保存 checkpoint、回報、測試與明列的外部副作用／未知結果；只有成功後才放開自己的 claims。它不負責自動停止程序或啟動新 worker。新的 dispatch 會指向包的路徑，不更改舊訊息收件人。

tmux worker 在明確 start 時建立 execution generation，首次派工可使用 pending session；只有通過已登記 pane 祖先鏈驗證的初始 hook 才能綁定真實 session ID。重啟或新的 SessionStart 不繼承舊 attempt，舊 claim 留待查證。這使 Codex 首個 prompt 前也能排程派工，不需要先發暖身模型回合。

## 專案登記與外部操作

`project check` 由 task.project 決定綁定範圍；核對 canonical repo／worktree、connector、account、workspace、destination、action 與最新 revision。bind 預設 unconfirmed，--confirmed 只記錄已確認的路由配置，revoke 保存取消紀錄。

`gateway prepare/preview/execute` 在呼叫 adapter 前重新檢查 task 與 binding，核對完整 payload digest、binding revision 和 adapter 設定版本，再持久記錄 sending。使用者的 adapter 負責以實際憑證查證遠端帳號／目的地，並只在確認成功後提供 receipt；未設定的通道不執行。詳見 [connector 契約](connectors.md)。

`project check` 單獨執行不保證稍後的外部操作。直接呼叫原始 connector 或網路會繞過 gateway，因此此機制不是不可繞過的 OS 隔離。當前 session 發訊授權及 connector preview 規則仍各自適用；票先留言、Slack 只通知查看。規劃清單、群組名稱與 worker 的 project 字串都不是目的地或發訊授權。

P0-2 的 `goal deliver` 以已確認專案授權執行 ticket.update → 原來源 reply，operation 額外綁定 delivery／來源／查證版本；gateway 在 sending 保留交易內重驗政策，reply 需先有 ticket receipt。這條路徑不要求重複人工批准，手動 gateway 仍依下方流程。完整契約見 [已授權完成回覆](authorized-delivery-spec.md)。

### Gateway 操作範例

以下在 agentctl repo 根目錄執行。先按 [connector 設定](connectors.md) 設定並啟用自己的 `slack.send` 命令，將範例 repo、account、workspace、destination 與證據換成已核對的值。所有命令須共用同一份 `AGENTCTL_HOME` 與 connector 設定。

```bash
bin/agentctl project add example-app --root /absolute/path/to/example-app \
  --repo /absolute/path/to/example-app
bin/agentctl task new "回覆原 ticket 的處理結果" --id notify-ticket \
  --project example-app --ticket /absolute/path/to/example-app/tickets/EXAMPLE-1.md
bin/agentctl project bind example-app --connector slack --account work-account \
  --workspace T12345678 --destination C12345678 --action send \
  --confirmed --evidence "已核對帳號與固定目的地的紀錄位置"
```

準備本機 JSON payload 檔，例如 `/absolute/path/to/message.json`。內容格式由自己的 adapter 定義；採用 `text` 欄位的 adapter 可使用：

```json
{"text":"我已在 EXAMPLE-1 更新處理結果，請到原票查看。"}
```

先更新原票，再準備與檢閱操作：

```bash
bin/agentctl gateway prepare --task notify-ticket --repo /absolute/path/to/example-app \
  --connector slack --account work-account --workspace T12345678 \
  --destination C12345678 --action send --payload /absolute/path/to/message.json
bin/agentctl gateway preview OPERATION_ID
```

`prepare` 輸出的 `id` 為 OPERATION_ID；確認 preview 中完整文字、帳號、目的地與 `adapter_available: true`。取得針對此內容的發訊授權後，將同一份 preview 的 `digest` 與整數 `binding_revision` 帶入：

```bash
bin/agentctl gateway execute OPERATION_ID --digest DIGEST --revision REVISION \
  --approved "此完整 preview 已獲發訊核可的來源紀錄"
```

`--confirmed` 只確認路由；`--approved` 記錄已取得的本次發訊核可，不提供新授權。設定或綁定變更後需重新 prepare；sending／unknown 需先查證遠端結果，不能直接重送。email／LINE 的步驟相同，替換 connector 名稱、目的地與 adapter 支援的 payload 即可。

## 回執恢復與停止

`goal delivery recover ID` 查證未知結果後接續；`goal delivery stop ID --reason TEXT` 阻止後續發送。獨立歷史查證用 `gateway inspect OPERATION`。connector 的唯讀查證契約、三次上限與不可保證去重的限制見 [P0-3 規格](delivery-recovery-spec.md)。
