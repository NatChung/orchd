# 新 Orch 系統決策紀錄（grilling 2026-09-29）

1. 基調：輕量。鎖放在權限層（OS/CLI 強制），流程不做會死鎖的狀態機。
2. 只鎖 Orch，目的是讓 Orch 不能自己做事、只能派工（也省 Astra token）。worker 權限全開。
3. Orch 家 = `~/projects/orch`，只能讀寫這裡；不碰任何專案 repo（含不讀 code/diff）。
4. worker 可直接用 nat-email / nat-slack / nat-line，不再經 nat-assistant 轉派。
5. Orch 介面 = MCP adapter（薄）→ 常駐 daemon（state、開停 worker、盯忙閒、叫醒 Orch）。
6. Orch = Desktop app（ChatGPT/Codex）中在 orch 專案開的 session，模型 GPT-6 Astra。
7. 多組：每個 UI session 就是自己那組的 Orch（與 ADR-0001 單一 Orch 相反，值得新 ADR）。
8. 一任務一 worker：派工開 worker + worktree；任務結束停 worker。
9. v1 worker 全部 Claude Opus 5.5；模型分流之後以規則（非 Orch 自選）加入。
10. 完成判定：Orch 依 worker 回報證據（commit、測試結果、未完成事項）對照派工時的完成條件；有風險才加派驗證 worker。回報格式（JSON 等省 token）之後設計。
11. 收尾：依 repo 設定；預設 push branch + 開 PR。push 完成且乾淨（無未 commit、本機=remote）即刪 worktree；否則保留並列出。
12. repo 待辦依各 repo `docs/agents/issue-tracker.md`，由 worker 處理。
13. Orch 自己的紀錄：GitHub issue（NatChung/orch），由 daemon 經 MCP 以 NatChung token 代跑 gh；只有跨 repo／多任務交辦才開。
14. 工具程式碼與 Orch 家分開：程式碼 repo `~/projects/orchd`（NatChung/orchd），Orch 家 `~/projects/orch`（NatChung/orch）。
15. 沒人管的組：不自動結束；`list_open` 列出所有未結束任務，由 Nat 手動關。v1 不做 adopt。
16. 對外寄送確認：worker 把預覽當問題送 Orch，Orch 原文轉給 Nat，答案轉回 worker；只暫停該步。
17. worker 前景／背景：沿用 agentctl attach（Claude `claude attach`、Ctrl+Z 離開）與 Ghostty 腳本；Orch 有 `view_worker` MCP 工具由 daemon 開視窗。
18. 新 repo，從 agentctl 複製需要的模組與測試；agentctl 不動。
    - 帶：Claude bg worker（--bg + UDS、job/session ID）、codex queue 叫醒 Orch、SQLite tasks/messages（ack/report、三種證據）、worktree 建立、Ghostty 腳本。
    - 不帶：tmux、scheduler/quota、goals、gateway/connectors、recovery、Codex worker、網頁看板。
    - 新寫：MCP adapter、多組、一任務一 worker 生命週期、push 後刪 worktree、repo merge 政策、Orch issue 工具。
19. `~/projects` 變回單純目錄，一次做完（博愛可重跑）。順序：備份 → 停服務（先查有無執行中 worker）→ 還原子專案 → 刪除。
    - 搬 orch/：PROJECTS.md、docs/handoffs/INDEX.md、recent-work.md、~/.codex/handoffs/projects/
    - 搬 orchd/：tools/projects-agent-mcp 未 commit 的 patch
    - 其餘刪除前打包 ~/.local/share/projects-orchestrator-final-backup-2026-09-29.tar.gz
    - 子專案：14 份 .codex/config.toml 去 project_agents 區塊；12 份 AGENTS.md 去 COMMUNICATION.md 連結；刪 untracked COMMUNICATION.md；tracked 的只改本機、不 commit/push，給 diff
    - 不動：projects-worktree-archive、~/.codex trust 設定、非 orchestrator 目錄、GitHub remote NatChung/codex-project-orchestrator
20. 名稱：orchd = NatChung/orchd，orch = NatChung/orch，private，remote 用 github-NatChung alias。

## 驗證結果（2026-09-29）
- ✅ Desktop app 會讀專案 `.codex/config.toml` 並可選其 permission profile（Nat 截圖：左下角 `orch`）。新版 orch profile 已於 trust 後實測通過（見下）。
- ✅ Claude Code 2.1.284：`claude --bg --messaging-socket-path <uds> --settings '{"crossSessionInbound":"accept"}'` 可啟動；對 UDS 寫一行 JSON 後 worker 收到並回覆 `ORCHD-UDS-OK`；`claude stop` 可停。worker 會把訊息標成「另一個 Claude session 送來」，並拒絕因 peer 要求而提權。
- ⚠️ `claude --bg` 要求工作目錄已 trust（未 trust 目錄直接拒絕）。worktree 位置要放在已 trust 的路徑下，或建立時處理 trust。
- ✅ 用 `codex queue --thread <ID>` 叫醒 Desktop app 的 Orch：可行（2026-09-29 實測）。Desktop app 雖然用自己內建的 app-server（stdio 接 app），`codex queue` 送到共用 daemon，但訊息仍出現在 Desktop 對話中，Orch 也回覆了。先前「會另跑一回合、app 看不到」的推測是錯的。
- ✅ trust：Orch 家 `~/projects/orch` 有自己的 `.git`，被視為獨立專案，必須在 `~/.codex/config.toml` 另外 trust，否則不載入專案 config（實測時變成全域唯讀）。trust 後 7 項權限測試全部符合預期。
- 待設計：daemon 如何取得「發出這次派工的 Orch session ID」。session 的 rollout 在 `~/.codex/sessions/`，內含 cwd 與 id。

## 延後
- 回報格式（省 token）
- 模型分流規則

## v1 實作（2026-09-29）
- **沒有常駐 daemon**（偏離決策 5）：`dispatch` 由 MCP server 直接開 worker；worker 跑 `orchd report/ask` 時寫 DB 並用 `codex queue --thread <派工的 Orch>` 叫醒 Orch。之後需要背景監看（例如偵測 worker 死掉）再加 daemon。
- Orch 身分：`dispatch` 記下 tools/call `_meta.threadId`（Desktop 0.158.0-alpha 與 codex exec 0.159 均實測有帶）。
- worktree：`~/projects/.orchd-worktrees/<repo>-<task>`，branch `orchd/<task>`，從 `origin/HEAD` 開。
- Claude trust 以 repo 主目錄為準（worktree 繼承；`~/projects` 底下的新 repo 不繼承）。dispatch 前檢查 `~/.claude.json`，未 trust 就拒絕並請 Nat 手動 trust；orchd 不自己改 `~/.claude.json`（多個 Claude 程序同時寫會互蓋）。目前未 trust 的 repo 例如 yite-hub、karaoke-hub、skills、sharon。
- state：`~/.local/share/orchd/orchd.db`（`ORCHD_HOME` 可覆寫），在 Orch sandbox 之外。

## v1.1 候選（v1 不做）
- Orch 的 GitHub issue 工具
- 各 repo merge 政策設定檔
- 模型分流規則
- 回報格式（JSON 省 token）
- adopt
- 背景監看 daemon

## v1 實測（2026-09-29～30）
- Shell pilot（orchd-pilot，本機 bare remote）：worker 20 秒內 ack、commit、push 任務 branch、回報含 commit 與 ls-remote 證據；main 未動；`close` 停 worker、刪 worktree。啟動 worker 的程序結束後 worker 仍存活並回報。
- Desktop pilot：Orch 在 Desktop 以 MCP dispatch → worker 回報 → `codex queue` 成功送出 → Orch 讀 inbox、核對、close。
- 發現：`codex queue` 的訊息要等 Orch 目前回合結束才進來；Orch 若在同一回合等待，通知會延遲。已在 orch/AGENTS.md 規定派工後即結束回合。
- 未測：Desktop app 重啟後 worker 是否存活（本次 app 未重啟）。

## v2 設計討論（2026-09-30，只定方向、未實作）

目的：同時跑 Opus 5.5 Orch 與 Astra Orch 兩組，比較誰更適合當 Orch。

1. 維持兩層（Nat → Orch → worker）；Orch 可換、可並存。Orch 記 `kind`（codex / claude）與模型。叫醒 Orch 依 kind 分流：codex 用 `codex queue`，claude 用 UDS。
2. Claude Orch 用 `orchd orch` 啟動（帶 `--messaging-socket-path`、Orch id 寫入環境變數給 MCP server 認身分），用 `claude attach` 或終端機對話；沒有語音。權限用 `~/projects/orch/.claude/settings.json` 只開 `mcp__orchd__*`。`~/projects/orch` 要先在 Claude trust。
3. 兩組共用 Orch 家：`AGENTS.md`（Codex）與 `CLAUDE.md`（Claude）內容保持一致，比較才公平。筆記各組分檔（例如 `groups/<orch-id>.md`），共用檔只由 Nat 改；Orch 不 commit。
4. 兩組派到同一 repo 允許（worktree 分開）；`list_open` 在同一 repo 已有別組未結任務時提醒。
5. worker 模型不再寫死：`dispatch` 帶 `model`，由 Orch 從 orchd 設定的允許清單挑選，未指定用便宜的預設款；必填一句 `model_reason`。推翻第 9 條「以規則分流、不讓 Orch 自選」。
6. 粒度：worker 便宜時回饋迴圈回到 Orch。加 `followup(task_id, message)`（UDS 送給同一個 worker，保留 context），以及 `retry(task_id, model)`（換更強的模型、保留 worktree 和 branch）。需要全新視角（例如 review）才開新 worker。review worker 的模型不可弱於作者。
7. 派工規格改為有結構：`goal`、`scope`（含不做什麼）、`verify`（可執行指令）、`manual_checks`（真的無法 script 化的才放這裡）。
8. 驗證：
   - 驗證 script 先寫、先 commit，回報給 Orch 看（此時應該失敗）；Orch 確認後 orchd 記下 hash。可交給不同 worker 寫驗證與實作。
   - worker 自己跑驗證（在自己的環境）。要當證據的那次走 `orchd verify <task-id>`：wrapper 在 worker shell 執行鎖定的 script，把 exit code、output 尾段、當下 commit SHA 寫進 DB。
   - `report` 時 orchd 只核對：script hash 未變，且最後一次 verify 的 SHA = HEAD。orchd 不自己跑測試。
9. 紀錄採事件流水帳（append-only）：Orch（kind、模型）、每次派 worker（模型、model_reason、任務類型標籤）、followup、retry（從哪個模型升到哪個）、verify 結果和 SHA、ask 次數、review 結果、結局（merge / 放棄 / 擱置）、返工（`rework_of`＋誰發現：verify / review / Orch / Nat）、token 用量（Claude 讀 transcript、Codex 讀 rollout，收工時撈進 DB）、Nat 可選的 1–3 分。
10. 比對：之後加 `orchd stats`（SQL），看第一次 verify 就過的比例、每個任務的 retry／followup 次數、升級頻率和升級後是否通過、返工率（Nat 抓到的單獨算）、每個完成任務的成本、Orch 選模型準不準。不同 Orch 拿到的工作難度不同，要依任務類型分開比。
11. ADR-0002 需補：一組的 Orch 可以是 Codex（Desktop UI session）或 Claude（`orchd orch` 啟動）。

未定：review 用 `gh pr review --comment` 取代 `--approve`（同帳號不能 approve 自己的 PR）。

### v2 補充（2026-09-30）
12. 主要目標：同時有兩種 Orch（Astra 與 Opus 5.5）來比較。worker 的調整是配套，不是目標。
13. worker 第一階段只用 Claude，由 Orch 從 Sonnet 5.5（M）與 Opus 5.5（H）挑選。GPT worker（Luna，GPT 系裡唯一便宜一個數量級的；Sol 與 Sonnet 5.5 同價同級，暫不用）排在第二階段，前提是先做出 Codex worker。規則草案：L=Luna max effort（第二階段）/ M=Sonnet 5.5 / H=Opus 5.5；同一層 verify 失敗兩次就升一層；review 的層級不低於作者，優先換另一家。
14. Orch 家只放一份 `AGENTS.md`（Claude Code 與 Codex 都會讀），取代第 3 條的 AGENTS.md / CLAUDE.md 雙份。
15. 實作順序：
    - 步驟 1（先讓兩種 Orch 能跑）：Orch kind 與依 kind 叫醒、`orchd orch` 啟動、Claude Orch 權限與身分、Orch 家筆記分組、#1 中途進度指令、#2 長時間工作放背景。
    - 步驟 2（比較要有數據）：`dispatch` 帶 model（Sonnet / Opus）與 model_reason、事件流水帳。這要在開始比較之前做好，不然早期的數據會漏掉。
    - 步驟 3：驗證 script 鎖定與 `orchd verify`、`followup` / `retry`、`orchd stats`。
    - 第二階段：Codex worker 與 Luna。

## v2 步驟 1–2 實作與驗證（2026-09-30，branch `v2-two-orchs`）
- 已做：`orchd progress`（#1）、worker 規則禁止 peer messaging 並要求長時間工作放背景（#2）、`orchs` 表與依 kind 叫醒、`orchd orch` / `orch-stop`（Claude Orch）、Orch 身分（Claude 用 MCP config 的 env；Codex 用 `_meta.threadId`，第一次呼叫時登記）、dispatch 的 `model`（sonnet 預設 / opus）、`model_reason`、`task_type`、`rework_of`、`found_by`、`other_open_on_repo`、close 的 `outcome`、`rating`、事件流水帳（messages 表的 dispatch / answer / close / usage）、收工時從 transcript 撈 worker token。
- Claude Orch 的鎖（實測）：`--restricted --permission-mode dontAsk --strict-mcp-config --mcp-config <env 帶 ORCHD_ORCH_ID> --settings {allow: Read, Edit, Write, mcp__orchd}`。沒有 Bash，WebSearch 被拒，檔案工具只能用在 Orch 家。
- 實測發現：
  - socket 目錄必須是 0700，否則 session 在 init 前就退出。
  - Claude Code 呼叫 MCP 時不帶 threadId，只能靠 env 認身分。
  - `--bg` session 由 Claude daemon 產生，啟動它的 shell 的環境變數傳不進去；只有 MCP config 裡的 env 會到 MCP server。
  - Orch 家的 `AGENTS.md` 不會自動載入，改用 `--append-system-prompt` 傳入。
- 端到端（2026-09-30，pilot repo + 本機 bare remote，Orch 與 worker 都用 Sonnet）：Claude Orch 派工 → worker ack → progress → commit、push → report → 用 UDS 叫醒 Orch → inbox → close（outcome=done）→ usage 已記錄。測試資料已從 DB 刪除。
- 行為改變：Codex Orch 的 MCP server 重啟後，`dispatch` 必須帶 `model_reason`、`task_type`；沒指定 model 時 worker 預設 Sonnet（原本寫死 Opus）。
- 未做（步驟 3）：驗證 script 鎖定與 `orchd verify`、`followup` / `retry`、`orchd stats`、Orch 本身的 token 統計（Claude Orch transcript / Codex rollout）。

## Codex worker 與 GPT-6.1 Sol（2026-09-30，提前自第二階段）
- 修改第 13 條：GPT-6.1 Sol（9/29 發布）改變「Sol 與 Sonnet 5.5 同價同級，暫不用」的判斷：同為 $2/$10，但 cache read $0.10（Sonnet $0.20），DeepSWE 75.2%（Sonnet 71.0%）。worker 改為 M 層有兩家：`sonnet` 與 `sol`（`gpt-6.1-sol`），`opus` 保留作 H 層與升級。預設仍是 `sonnet`：Sol 太新、還沒有第三方實測。
- Luna 延後：GPT-6 Luna 便宜（$0.10/$0.50），但 Terminal-Bench 4.0 只有 13%，worker 主要做 shell/git/PR，不適合。有 Codex worker 後加 Luna 只需在 `MODELS` 多一行，要用時拿實際任務比較。
- 設計：`codex exec` 沒有 `--bg`，一次只跑一個回合就結束。所以 Codex worker 是同一個 thread 上的一串程序：派工時 `codex exec --json`（brief＋任務放在同一個 prompt，因為沒有 `--append-system-prompt`；不寫 AGENTS.md 進 worktree，否則 worktree 一直是 dirty），回答時 `codex exec resume <thread>`。`session_id` 存 thread id，`job_id` 存目前回合的 pid，`socket` 為空。沒有另開欄位，worker 種類由 model 前綴 `gpt-` 判斷。
- 跟 Claude worker 的行為差異：
  - `orchd ask` 之後 Codex worker 要結束回合（不是等待）；答案以新回合送達。回合進行中的 `answer` 改為排隊（#12，見下節）。
  - 長指令在前景跑、給足 timeout：回合結束程序就結束，背景工作結束時沒有東西叫醒它。
  - `list_open` 的 `worker_alive`：pid 還在 = true；已 ask / report 而沒有程序 = null（可 resume）；其他情況沒有程序 = false（回合中途結束、沒有回報）。
  - `view_worker` 回合中會被拒；回合之間開 Ghostty 跑 `codex resume <thread>`。
  - token 從 `~/.codex/sessions/**/rollout-*-<thread>.jsonl` 最後一筆 `token_count.total_token_usage` 撈，對應到 Claude 的欄位（codex 的 input 含 cached，要扣掉）。
  - Claude 的 trust 檢查只套用在 Claude worker。`orchd orch --model` 只接受 Claude 的 model。
  - `exec resume` 不帶 `-m` 會改用 config.toml 的預設 model（實測變成 Astra），所以每次 resume 都帶任務的 model。
  - MCP server 是長時間跑的 process，spawn 出來的 codex 結束後會變成 zombie，`kill(pid, 0)` 仍然成功；`pid_alive` 先用 `waitpid(WNOHANG)` 收掉。
- 端到端（2026-09-30，scratch repo＋本機 bare remote，worker 為 GPT-6.1 Sol）：dispatch → ack → commit、push -u → report；另一個任務 ask → answer 走 `exec resume` → report → close（worktree 移除、usage 已記錄）。Orch thread 是假的，所以叫醒 Orch 記為 wake_error，符合預期。review 修正後在同一個 process 內重跑 ask → answer：兩個回合都是 gpt-6.1-sol。還沒從真正的 Orch（MCP）派過。

## Codex worker 回合中的 answer 排隊（#12，2026-10-02）
- 問題：`codex exec` 回合中收不到訊息，`answer` 等 60 秒後報 `still in a turn`，Orch 的指示送不進去。
- 決定（Nat 選 A：lazy flush）：沒有 daemon、沒有 per-turn wrapper；排隊的答案只在 Orch 之後再呼叫 `answer` 時送出。
- 交付契約：`answer(task_id, text?, flush?)` 回傳 `{status, delivered, pending}`。
  - `queued`：已存進 DB，worker 還沒看到；task status 不變。
  - `delivered`：沒有任何答案在排隊（`delivered` = 這次呼叫送出的則數，可能是 0：例如 flush 時沒有東西、或另一個呼叫已經一起送出）。
  - `failed`：`exec resume` 起不來（附 `error`）；所有答案仍在排隊，用 `flush=true` 重試，不要重送 text。
  - `text` 與 `flush` 至少要有一個；`flush=true` 不帶 text 也可用來查 pending。Claude worker 行為不變（UDS 直送，失敗照舊拋錯；`flush` 無事可做，回 delivered 0）。
- 送出規則：仍保留最多 60 秒等待（`orchd ask` 叫醒 Orch 時 worker 回合還沒退出的 race）；回合已結束就把所有排隊答案依 FIFO（舊到新）加上本次 text，合成一個 resume 訊息，每段前綴 `[orchd answer <id>]`，只開一個回合。
- 持久化：沒有 schema migration。排隊答案是 `messages` 的 `kind=answer_queued` 列，`read_at` 為送出時間（NULL = 待送）；送出時再照舊寫 `kind=answer` 列。`answer_queued` 不在 `ORCH_KINDS`，不進 Orch inbox。MCP server / 機器重啟後排隊仍在。
- 不重複、不覆蓋：檢查 worker 是否閒置、領取排隊列、spawn resume、更新 `job_id` 都在 task 的 delivery flock 裡；兩個 MCP process 同時 flush 只會有一個開回合。spawn 失敗就什麼都不寫（answers 仍排隊）。
  - PR #32 返工（Nat 選 A，2026-10-02，plain answer 與 followup 共用）：resume 的 spawn 不再包在 `BEGIN IMMEDIATE` 裡（之前整段持 SQLite 寫鎖跨 spawn）。spawn 之後才開一個短交易，照 retry 的 compare-and-set：task 未 closed、model / session / 先前 `job_id` 沒變、送出的排隊列都還沒讀，才寫 `job_id`、`acked`、read_at 和 `answer` 列。這個交易失敗（DB 錯誤或被別的寫入搶先）時，用 close 的 `stop_task_worker`（只認這個 pid＋worktree／thread）停掉剛開的 worker，answers 留在 queue，回 `failed` 加 `uncertain: true`；停不掉時 error 寫明 job id 和 `MAY STILL BE RUNNING`，並盡量把 pid 記到 task，它還活著時下一次 flush 會回 queued，不會同時開第二個 actor。兩種情況那些 answers 都已經到過那個 worker 但仍顯示 pending，之後 flush 會再送一次；error 寫明這點，Orch 先看 log 或等它的 report 再決定。task status 只在 spawn 後沒被改過時才改成 `acked`，resume 起來的 worker 若已經 ack / report，保留它寫的 status。之前這個情況是丟例外、worker 沒被記錄、answers 仍未讀，下一次 flush 會重送一次。成功、spawn 失敗、busy、closed 的回傳都和之前一樣。
- close：已 close 的 task 不收也不送；排隊中的答案保留在 DB（可查），永遠不 dispatch。鎖內會再檢查一次 status。
- Orch 的責任：看到 `queued`，等該 worker 下一次 progress / ask / report 叫醒並讀完 inbox 後，呼叫 `answer(flush=true)`（或帶新 text）。例外：task 在 `question` 狀態時拿到 `queued`，表示 worker 已經 ask、只是程序還沒退出，之後不會再叫醒 Orch；Orch 要稍後主動 `flush=true`（或等 `list_open` 的 `worker_alive` 變 null）。寫在 MCP `answer` 工具描述裡；`inbox`、`list_open` 不顯示 pending（不在 #12 範圍）。
- 沒做：不自動送出（worker 回合結束後沒有東西觸發）、沒有 TTL / cancel、沒用 `codex queue`（只在 Desktop session 驗證過，對 `exec` thread 未驗證）。

## followup（#5 剩餘，2026-10-02）
- 契約：`followup(task_id, message)` 對未 closed 的既有 task 追加指示；同 task / worktree / branch / session / owner，不換模型（換模型用 `retry`）、不自動 retry、不建新 task。missing（KeyError）、closed、空 message 一律拒絕，不寫 event。
- 交付：走 `answer()` 的交付邏輯（內部 `_answer(..., accept=)`，私有 hook，公開 `answer` 行為不變）。Claude worker 在 task lock 下 `send_uds`；Codex worker 回合中則進同一條 `answer_queued` FIFO（status `queued`），之後 `answer(flush=true)` 與其他排隊答案依序合成一個 resume 回合，不重開 worker、不丟 queue。
- 接受邊界（PR #32 返工）：`followup` 的「接受」一定在拿到 task delivery flock 之後，緊接 closed 重檢查、在任何 queue / send 之前寫 `kind=followup` 事件（`status=accepted`）。**拿不到 lock（close / retry / adopt 進行中，含 close 已 stop worker 但 status 尚未 closed）→ 拋 `TimeoutError`，不寫事件、不 queue、不 send，Orch 稍後重試**；這和 plain `answer`（lock 忙時仍 queue + error）刻意不同。Codex：短暫持 lock 做「closed 重檢 + 事件 + queue 列」（一個短 SQL 交易，不跨 network），釋放後才走既有 flush 路徑（不重入 flock）；之後 lock 變忙 → 回 `queued` receipt（已接受，之後 flush 會送）。接受事件寫入失敗 → 不送。
- 結果紀錄：送出後把同一列事件更新成交付結果（delivered / queued / failed + pending / error）。送出後的事件更新失敗不拋例外（指示已送出）：回傳 receipt 帶 `record_error`，事件列維持 `accepted`。
  - 事件身分（PR #32 二次返工）：事件 JSON 多一個隨機 `token`。更新事件是 compare-and-set：同 id、同 task、`kind=followup`、body 和接受時寫的完全一樣才寫。接受列若跟著 queue INSERT 一起 rollback，那個 AUTOINCREMENT id 可能被別的程序的 report 拿去用；這時更新對不到任何列，不會蓋掉別人的資料，原本的錯誤照常拋出。
  - 接受後的每一次取鎖失敗都回已接受的 `queued` receipt：Codex 第一次 flush 拿不到鎖，以及 retry 把 task 換成 Claude 後再拿一次鎖拿不到（`_ToClaude`）。Claude worker 的接受和送出在同一把鎖裡，沒有第二次取鎖。plain `answer` 在 `_ToClaude` 拿不到鎖時照舊拋 `TimeoutError`。
  - Claude 送出成功但 receipt 交易失敗（read_at / `answer` 列 / `acked`）：followup 回 `delivered`，`record_error` 說明 receipt 沒寫入；若這次送出也帶了 Codex 留下的排隊列，列出那些 id，並說它們仍顯示 pending、不要再 flush。事件記成 `delivered` + `record_error`。plain `answer` 照舊拋例外。是否 closed 的判斷是結構化的（事件列存在與否），不看例外文字（transport 的 `OSError('connection closed')` 照常記 failed）。
- 訊息：以 `[orchd answer <id>]` 包裝（worker 已認得），內文開頭 `[followup]` 說明這不是回答、請 `ack`、可 `progress`、最後新 `report`。交付後 status 回 `acked`；原本的 report / evidence 列不動。
- 紀錄：每次被接受的請求寫一列 `kind=followup` 事件（JSON：message、from_status、model、session_id、status、pending/error）。不在 `ORCH_KINDS`，不進 inbox；`orchd stats` 已經在數這個 kind。worker 的第二次 report 照常叫醒 Orch。
- 沒做（#5 仍開）：Codex Orch `approval_mode` 加 `retry` / `followup`、更新 Orch AGENTS.md（皆為全域 / 共用檔，待 Nat 決定）、真 worker pilot / E2E。沒有 CLI 子命令（`retry` / `answer` 也沒有）。已知限制：送達不是 exactly-once —— Claude 的 `send_uds` 與其 receipt 是分開步驟；followup 在 receipt 失敗時回 `delivered` + `record_error`，但 DB 的 task status 和排隊列沒更新，之後 plain `answer(flush=true)` 仍可能把那些列再送一次。已接受並 queue 的 Codex 指示，若之後 task 被 Nat 正式 close，queue 列保留在 DB 但永不送出（與 queued answer 相同；accepted ≠ delivered）。

## 驗證鎖與獨立同 SHA 重跑（#4，2026-10-02）
- 決定（Nat 選）：hash 鎖＋由另一個 worker 在同一個 SHA 重跑。作者自己跑過不算驗收；同一個本機 UID 下只做到 tamper-evident，不是 tamperproof，Orch 要再核對驗證 worker 自己的回報。
- `dispatch` 多三個可選欄位：`verify`（指令）、`manual_checks`、`verifies`（要驗證的作者任務 id；必須存在且未 close）。新增三個 nullable 欄位，沒有 migration；dispatch 事件只在有值時多帶這些 key。
- `lock_verify(task_id, paths, command?)`（MCP，Orch 在作者回報之後呼叫）：讀作者 worktree（不寫），記下目前 HEAD、每個 path 在該 HEAD 的 git object hash、指令（預設 dispatch 的 `verify`）。拒絕：任務已 close、驗證任務本身、worktree 有未 commit 或 untracked 檔案、路徑為絕對路徑或含空段／`.`／`..`／`.git`／反斜線、該 commit 沒追蹤、路徑或其上層是 symlink／submodule、目錄底下有 symlink／submodule。路徑從 `git ls-tree` 比對，不碰檔案系統。再鎖一次會回傳 `changed_paths`（跟上一個鎖不同的 hash），可抓到「先鎖測試、之後被改弱」。
- `orchd verify <驗證任務 id>`（驗證 worker 自己跑）：拒絕沒有 `verifies`、驗自己、作者已 close、作者沒有鎖、兩個任務在 DB 記的 `session_id` 相同、自己的 worktree 不乾淨；拒絕時不 checkout。`session_id` 比對的是任務紀錄，不是實際執行 CLI 的身分：在作者的 worktree 跑 `orchd verify V` 也會記成 V 的結果，orchd 沒有任何指令檢查呼叫者（同 UID 限制）。之後在自己的 worktree `checkout --detach` 鎖住的 SHA、重算 hash（`hash_ok`）、跑鎖住的指令、記 exit、輸出尾段、`cleanup`、`dirty`（跑完 `status --porcelain` 有東西）、`head_after`，寫成作者任務的 `verification` 訊息；乾淨才切回原 branch，dirty 就留著不丟。exit 0 pass／1 fail／2 拒絕。
  - 指令在新 session（自己的 process group）裡跑，輸出寫暫存檔（不用 pipe，背景子程序拿著 pipe 會卡住讀取）。group 的 leader 是 orchd 起的 holder（`sys.executable -I -c`，純 Python）：它跑 `/bin/sh -c <指令>`、自己回收 shell、把 exit status 經 pipe 回給 orchd，然後卡在 stdin 不退出。shell 結束（或 timeout）時，orchd 趁 holder 還活著（或還是 orchd 沒回收的 zombie）——也就是 group id 還被保留——對整個 group 送 SIGKILL，之後才放掉並回收 holder，所以不會殺到無關的程序；回收後只用 signal 0 確認 group 已空（最多 `CLEANUP_WAIT` 5 秒）。成功、失敗、timeout 都走這條。指令自己把 holder 殺掉時，exit 記 null（fail），group 照樣被殺（macOS 實測：zombie holder 仍保留 group id）。
  - 支援：POSIX（macOS、Linux）＋ Python 3.9 以上（2026-10-02 在 macOS 的 `/usr/bin/python3` 3.9.6 和 Homebrew 3.14 都跑過）。不用 `os.waitid`（macOS 上 3.13 以前沒有；PR #31 第一版用它，3.9 會在 checkout 之後 AttributeError、把驗證 worktree 留在 detached）。`run` 一開始先檢查 `os.name == "posix"`、`os.killpg`／`setsid`／`pipe`、`select.select`、可執行的 `sys.executable` 和 `/bin/sh`；缺任何一個就 exit 2 拒絕，什麼都不 checkout、不寫紀錄，訊息列出缺什麼和支援範圍。Linux 沒實測。
  - group 確認清空（`cleanup: true`）之後才判 `dirty`／`head_after`、才切回 branch。確認不了就是 fail，`dirty`／`head_after` 留空、不切回（留在鎖住的 SHA），`restore_error` 寫明原因。
  - checkout 之後整段包 try/finally：任何例外都會寫一筆帶 `error` 的 fail 紀錄再往上拋（CLI 印 traceback、exit 1，跟 fail 同碼，不是 exit 2 的拒絕）；只在 group 已確認清空（或指令還沒開始）且 tree 乾淨時切回，用一般 `git checkout`（不加 `-f`，git 會拒絕覆蓋）。切回失敗記在 `restored: false`＋`restore_error`，紀錄和回傳都有。
  - 抓不到：自己 `setsid`／`setpgid` 離開 group 的子程序（daemon 化），它們跑完後還是能寫驗證 worktree。
- 判定 `verify.status`：`none`（沒鎖）／`locked`（還沒有獨立重跑）／`pass`／`fail`／`stale`（作者 branch 目前的 tip ≠ 鎖的 SHA，任何新 commit 都算）。pass 要同時符合：對應最新的鎖、exit 0、hash_ok、`cleanup` 為 true、`dirty` 為 false、沒有 `error`、HEAD 沒變、驗證者 ≠ 作者、作者 tip = 鎖的 SHA。DB 裡記錄的 `passed` 不採信，每次重算。作者 tip 讀 `refs/heads/<branch>`，worktree 刪了也查得到。本機 `pass` 不等於 PR 驗收：別的 clone push 或 GitHub「Update branch」之後，本機 branch 還是舊的。Orch 要比對 `lock_sha` 和目前 PR head，並讀驗證 worker 自己的回報，才能當成 PR 驗收。
- `inbox` 每筆多 `verification`（驗證任務顯示它驗證的那個任務，`role: verifier`）。沒有鎖也不是驗證任務就直接 `{state: none}`，不跑 git。
- 紀錄是 `messages` 的 `verify_lock`／`verification` 列（跟 `answer_queued` 一樣），不在 `ORCH_KINDS`，不改任務生命週期，也不動 `report`。
- 沒做：真正兩個 worker 的實測、MCP server 重啟後新工具才會出現（不在本票）、自動派驗證 worker、從 PR 讀 head SHA（以本機 branch tip 為準）、worker brief 沒加 verify 說明（派工指示要寫明跑 `orchd verify <自己的 id>`）。
