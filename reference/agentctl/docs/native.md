# native 管理

## 已定案行為

- 預設 native，可明確選 tmux。整組不可混用，切模式前停止整組。
- 終端視窗與背景工作分離；attach 看同一 session、可手動輸入、不自動 manual。
- 明確 `agentctl stop` 停止整組；關 terminal 不停止。重新啟動預設新對話，`--resume` 才恢復。
- Orch 故障時保留 workers 的目前工作及回報、停止新派工，等待手動恢復。此項採實作啟動時告知使用者的建議值。
- 每個 worker 一次一則未完成派工；回報且 idle 後才送下一則。手動插話由 CLI 自己處理。
- 回報存在 SQLite inbox，Orch idle 時以原生訊息喚醒；沒有前景視窗也運作。
- 傳送錯誤直接記錄，不 fallback、不自動重送。

## 授權提示與版本切換

P0-1 的 native／tmux 提示共用 goal 政策規則：每次查 `goal show`，automatic 保留核驗、human 等指定人驗收；匹配已批准 standing grant 的例行交辦不需重複詢問。既有 legacy goal 不自動遷移。詳見 [政策操作](goal-coordination.md#專案政策管理p0-1)。

舊 session 提示與已載入 runtime 不會熱更新；切換新程式需另外安排正常停止／啟動管理組，本次實作沒有操作正式 sessions。送出授權與 remote receipt 仍分開；P0-2 已提供 `goal deliver`，新提示要求 achieved 後依有效授權更新 ticket／回覆，操作見 [完成回覆規格](authorized-delivery-spec.md)。

## 結構

2026-09-09 新增的 scheduler 使用同一 native pump，不另開派工通道。先明確 configure／resume；每次以 transaction 認領 task、worker slot 與 cwd，再持久保存 sending 後呼叫原生 CLI。attempt 綁定 native generation／session；report 只進 review，須有 report 後同 generation 的新 idle observation 才釋放資源。未知結果、舊 generation 或不明停止不會自動重派。queue、交接包與恢復指令見 [README](../README.md#專案登記任務-worktree-與持久排程)。

scheduler 啟用後，Codex observer 每 60 秒透過既有唯讀 adapter 刷新額度；不需要 viewer 或模型回合。刷新失敗不停止 observer，只將 quota 標示未知。升級程式後，既有背景 observer 不會熱更新，需正常停止／啟動管理組才使用新的排程流程；不要直接刪除 state。

真實 Codex／Claude 短排程任務均通過，版本、隔離與清理範圍見 [最新驗證](next-steps.md) 及 [證據](evidence/scheduler-live-2026-09-09.json)。2026-09-10 新增逐任務 opt-in recovery：新鮮可信額度耗盡且 current dispatch 已 ack 時，持久保存停止工作與程序身分，停止已觀察 writer。人工確認 detached writers／未知外部結果後，才製作 handoff、建立同 cwd Claude worker 並 retry。這套流程僅有隔離測試，沒有宣稱真實耗盡交接、全自動停止證明或 Orch 接管已完成。命令與中斷後 abandon 見 [README](../README.md#耗盡後的受管-recovery)。

訂閱額度由 `bin/quota.py` 保存為 `quota_observations`，與 worker 忙閒及 task 完成狀態分開。新 Codex native session 啟動後會另以短命唯讀 client 連到已登記 App Server 查詢額度；查詢失敗只標記 quota unknown，不使任務 observer 失敗。`account/rateLimits/updated` 使舊觀察待刷新，不把 sparse event 當完整 snapshot。`quota --refresh`／`watch --refresh` 可再次讀取，沒有模型回合。

Claude 新啟動時在可解析的 user/project/local/CLI 設定上串接 session-only statusLine collector，將原本命令存進該 generation 的私人設定檔並原樣轉送 stdin/stdout。工作目錄 trust 與既有 hooks/managed policy 繼續適用；不改寫或繞過它們。無法可靠解析設定時只略過 collection，CLI 原本設定仍保留。舊 generation 的 callback 被拒絕，避免重啟後污染新觀察。使用 `quota` 查看來源、錯誤與新鮮度；不把 Claude callback 時間冒充 upstream 更新時間。

`bin/native_runtime.py` 封裝 provider 生命週期、接回命令、傳送與狀態觀察。`bin/agentctl` 共用既有 tasks/messages/inbox，`management` 記錄整組模式，`native_sessions` 保存每個工作程序的 generation、session/job ID、PID＋啟動時間、socket 與錯誤紀錄。

Codex：每個 Agent 啟動私有 `codex app-server --listen unix://PATH`。觀察者維持 WebSocket 訂閱，以 thread/start 或 thread/resume 建立／恢復對話；turn/item 事件更新忙閒及收件。前景用原生 `codex resume --remote`，派送用 `codex queue`。所有 socket 都在短路徑的私有 `/tmp/agentctl-*` 目錄，避免 macOS 路徑長度限制。

Claude：`claude --bg` 配合每次啟動專用的 UDS 路徑與 `crossSessionInbound: accept`，設定只套本次 session，不改全域 settings。啟動 stdin 明確設 DEVNULL，避免 --bg 把呼叫端 stdin 誤讀成 prompt。觀察者讀 `claude agents --json` 的 session/PID/忙閒狀態；未知狀態停止派工，不猜作 idle。attach 與 stop 使用確切 background job ID。

兩種接收方都要求先 `agentctl ack <message-id>`，再執行，最後明確 `report`。Codex 的 userMessage 事件也可提供收件證據。UDS socket write 只代表傳輸，不能當模型已讀。native 不靠舊 project hooks 判斷身份，觀看用 TUI 的 hook 不得接管或停止整組。

傳送前用 SQLite `sending` 保留訊息並提交，之後才執行原生 I/O，避免接收方 ack/report 與發送端鎖互等。完成 I/O 後只把仍在 sending 的訊息設 sent，保留提早到達的 ack/report。60 秒無收件證據設 unconfirmed，繼續阻擋後續 dispatch；明確 resend 才重新排隊。

## Socket 與 session ID 定位

每次啟動先建立私有 `/tmp/agentctl-<隨機值>/session.sock` 路徑，並以 worker ID 登記在 SQLite `native_sessions.endpoint`。socket 由對應 provider 建立；agentctl 不依 cwd 猜接收方，也不掃描既有 sessions 自動接管。

1. **Codex**：啟動 `codex app-server --listen unix://<endpoint>`，連線完成 initialize 後呼叫 `thread/start` 或 `thread/resume`。把回傳的 `thread.id` 存入 `session_id`（`job_id` 同值）。新 thread 隨即用 `thread/name/set` 設定名稱，再做一次完整 `thread/resume`，建立空白對話及其分頁歷史來源；兩步都不啟動模型 turn。完成後才宣告 idle，供 TUI 使用 `excludeTurns: true` 接回。派送使用 `codex queue --remote unix://<endpoint> --thread <session_id> --message <text>`；接回使用 `codex resume --remote unix://<endpoint> --cd <worker-cwd> <session_id>`。
2. **Claude**：啟動 `claude --bg --messaging-socket-path <endpoint> --settings '{"crossSessionInbound":"accept"}'`。從輸出的 `claude attach <job-id>` 取得 job ID，再從 `claude agents --json` 找 `id` 相同的項目，等其 `sessionId` 與 socket 就緒，存下 session ID 與 PID。接回與停止分別使用 `claude attach <job-id>`、`claude stop <job-id>`。

Claude 派送直接連登記的 UDS，寫入一行 JSON（下列為格式示意）：

```json
{"type":"user","session_id":"<session-id>","uuid":"<每次傳送的新 UUID>","from":"agentctl","priority":"next","message":{"role":"user","content":"<訊息>"}}
```

因此發送方是 Claude 或 Codex 都不影響路由：查接收方的 `kind` 決定 queue 或 peer UDS。日常操作使用 `agentctl send <worker-id> ...`，由管理層保留派工紀錄再呼叫傳輸。

`generation` 區分每次啟動，PID 與啟動時間用於確認程序身分。resume 後重新記錄 provider 回傳的 ID；Claude 的 job ID 與 session ID 是不同欄位，不能互換，也不能假設 resume 後不變。已退出的 session 紀錄用於查證或明確 resume，不能僅因紀錄存在就派送。

## 操作與失敗處理

- `status` 顯示 backend、session、錯誤與 log 路徑；Codex `worker screen` 顯示最後完整回覆，完整歷史用 attach。Claude screen 呼叫原生 logs。
- `resend` 用於查證後重試 dispatch 或 wake；已處理的工作應 report，而非重送。
- `inbox --mark-read` 是 native Orch 確認收到回報的入口，同時結束待處理 wake。
- 觀察者或 provider 不在時拒絕派送。停止用 PID＋啟動時間驗證，防止 PID 重用誤殺；Claude 用精確 job ID，Codex 只停止私有 server。
- 停止失敗會報錯並保留狀態；不宣稱清理完成，也不允許模式混用。
- runtime logs 在 `$AGENTCTL_HOME/native`，session 對應在 `$AGENTCTL_HOME/agentctl.db` 的 `native_sessions`；既有紀錄保留以供 resume／查證。重開機後重新 start，必要時 --resume。

## 驗證

自動化測試：`python3 -m unittest discover -s tests`，原有 59 項通過；空白 Codex 接回修正後，以 `AGENTCTL_TEST_CODEX_LIVE=1 python3 -m unittest discover -s tests` 跑完整 61 項通過（包含真實 App Server；未設定環境變數時跳過該項）。包括 tmux 既有測試與 native 的 busy/idle 排隊、先 ack 後 sender 返回的競態、未知送達不重試、Orch 故障暫停新派工、模式互斥、觀看 hook 隔離、PID 重用及 WebSocket framing。

2026-09-08 隔離 AGENTCTL_HOME 與臨時測試 repo 的真實驗證：

- Claude 背景 worker 以 UDS 收到任務，執行 ack/report；回報透過 queue 喚醒 Codex Orch，Orch 讀取 inbox 並確認 HELLO。
- Codex 背景 worker 以 queue 收到任務並 ack/report；Codex Orch 自動讀取回報。
- Claude Orch 收到 UDS 交辦，實際透過 agentctl 派給 Codex worker；worker 回報後，Claude Orch 收到 UDS wake 並讀取 inbox。
- 獨立 PTY 接回 Codex worker 與 Claude Orch，看到既有 HELLO 對話；關閉觀看連線／Claude Ctrl+Z 後，背景工作保留。
- Claude 前景留有未送出草稿時，native dispatch 仍完成 ack/report；detach 後重新 attach，原草稿仍在。
- 明確 stop 後，兩種 provider 的程序均結束；--resume 後 Codex 保留 session ID。Claude 2.1.263 的 --bg --resume 在本次測試建立了新的背景 job/session ID，但保留對話歷史：新的 session 能正確報出停止前的 HELLO 標記。agentctl 記錄新的 ID 供後續 attach 與傳訊，不能假設跨停止／resume 的 Claude ID 不變。

仍有版本相依的人工 UI 邊界：保留草稿由原生 CLI 負責，agentctl 不送模擬按鍵；不同 CLI 的 /exit 可能退出底層工作，請使用原生 detach（Claude Ctrl+Z）或關閉觀看終端來離開前景，整組停止使用 agentctl stop。

## 版本相依

開發環境為 Codex CLI 0.153.4、Claude Code 2.1.263。Claude UDS frame 和隱藏的 messaging-socket-path 參數來自本機實作查證，可能隨版本變化；失效時報錯／unconfirmed，不自動切換通道。

Codex 使用官方 App Server 協定：https://learn.chatgpt.com/docs/app-server 。Unix socket 上是 HTTP Upgrade 後的 WebSocket，不是純 JSONL。系統仍使用使用者已配置的 CLI 登入與模型設定。

### 空白 Codex 接回修正（2026-09-08）

先前 Codex 接回驗證在 HELLO turn 後執行，未涵蓋完全空白 thread。實際新建且未發言的 worker 在 resume 時出現 `no rollout found for thread id`。已用真實 App Server 重現；`ephemeral: false` 或 paginated history 均無法修復，`thread/name/set` 能建立空白紀錄，但單獨使用仍不足以支援 TUI 的分頁歷史。

啟動流程先設定 thread 名稱並完整 resume，再宣告 idle；接回命令明確傳入 worker cwd。回歸測試 `tests/test_native_codex_live.py` 在隔離 server／目錄建立空白 thread，再以獨立連線連續用 `excludeTurns: true` resume 兩次，確認 ID、cwd 與零 turn。測試不啟動模型工作。

第二次實測發現 TUI 首次接回報 `invalid paginated history lineage: missing source rollout`。原測試先做完整 resume，無意間建立了缺少的歷史來源，遮蔽真實 TUI 的 metadata-only bootstrap 錯誤。已先將測試改成首次即 `excludeTurns: true` 並確認失敗，再修正初始化順序。單純等待 2 秒未改善；完整 resume 後才通過。重新建立受測 worker 後，直接用真實 PTY 執行 `worker attach` 驗證，不在驗證前額外做 API resume。

### Orch 收件地址修正（2026-09-08）

實際 Orch ID 為 `orch`，舊協定的邏輯收件地址為 `orchestrator`。原本 `report` 將回報送到派工的 sender，因此 `--from orch` 產生的回報存給 `orch`，而預設 inbox 與 wake 只查 `orchestrator`，造成 status 有回報但 inbox 空白且不喚醒。

現在預設 inbox、`inbox --for orch`、通知與 pump 共用地址解析，合併角色地址及目前登記的 Orch ID。舊回報原樣保留即可讀取；mark-read 只標記實際顯示的訊息，兩種入口都會結束 native wake，其他 worker／使用者收件匣不受影響。

新增從實際 Orch sender 派工、report、busy 排隊、idle 喚醒到預設 inbox 讀取的回歸測試，以及新舊地址合併／其他收件匣隔離測試。包含真實 Codex transport 的完整 63 項測試通過。

歷史真實驗證：Claude Orch 派出任務，Codex worker 回報後產生 wake，Orch 在 idle 後以 `inbox --mark-read` 讀取回報並結束 wake，再回報原始交辦。先前卡住的回報也經同一入口讀取，沒有使用 status fallback。私人訊息 ID 不隨分享版提供；此紀錄不代表目前使用者的 CLI 環境已驗收。
