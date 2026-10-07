---
status: accepted
---

# 只鎖 Orch，worker 權限全開

agentctl 與 codex-project-orchestrator 都試過用 sandbox／profile 同時鎖 Orch 與 worker。實際執行時 worker 常因 MCP、skill、外部檔案、computer use、browser 被擋，最後兩套都切回全開（agentctl 預設 bypass，projects 切 local 模式）；通訊還得繞 worker → Orch → example-assistant 才能寄 LINE。

鎖的真正目的只有一個：讓 Orch 不能自己做事、只能派工，也避免最貴的模型去讀 code。所以只鎖 Orch：Codex permission profile 限定只能寫 `~/projects/orch`、不碰任何專案 repo、關 shell network、plugins 與原生 subagent。worker 全開，可直接用通訊 skill；worker 之間靠「一個 worktree 一個寫入者」分開，對外寄送靠 skill 既有的預覽＋你的確認把關。

放棄的做法：worker 也鎖（反覆卡住）；Orch 可讀所有 repo（會變成貴模型做 worker 的事）。
