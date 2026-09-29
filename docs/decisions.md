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
