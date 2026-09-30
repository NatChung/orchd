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
