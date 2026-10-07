---
status: accepted
---

# 每個 Orch session 自成一組，可同時多組

agentctl ADR-0001 採單一 Orch。但 Orch 在 ChatGPT Desktop app 開啟，UI 點一下就開新 session，無法保證只有一個。考慮過「固定 thread＋明確接手」與「UI session 只轉送給背景 Orch」，都多一層身分或轉手成本。

改為：在 Orch 家開的每個 session 就是一組的 Orch，派出的 workers 屬於該組；daemon 在所有組之間仍保證一個 worktree 只有一個寫入者。沒人管的組不自動結束（daemon 分不出「丟著」與「還沒回來看」），改由未結任務清單列出、由你手動關閉。

代價：同一 repo 可能被不同組同時改，衝突在 PR／merge 時處理；worker 回報要送回派工的那個 session，需要能定位該 session（見 decisions.md 待驗證項）。
