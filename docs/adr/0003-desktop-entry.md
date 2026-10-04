---
status: proposed
---

# Desktop 入口：原文轉交固定的 Claude Orch（#37）

ADR-0002 讓每個 Desktop session 自成一組，也曾以「多一層身分或轉手成本」放棄「UI session 只轉送背景 Orch」。
#37 要 Nat 在 Codex Desktop 交辦與回答追問，同時讓理解、排程、派工都留在一個固定的 Claude Opus Orch，
所以為**入口**接受這份成本：入口是新角色，不是 Orch，不開組。

- 入口資料夾是 `~/orch/interface`（`orchd init` 建立）。入口用 `orchd mcp --role entry` 啟動：只列出、只接受 `relay`／`status`，參數只有 id，不登記成 Orch。
  其他 Desktop session（沒有這個 role）照 ADR-0002 自成一組，不受影響。
- 綁定由 Nat 手動做（`orchd binding`，或 `orchd binding --to ORCH_ID` 指定），只接受在線的 Claude Orch；不會自動換綁。
  綁定與待答問題存在 SQLite，入口重開（新 thread 也一樣）接回原 Orch。
- 原文不經模型重打：Nat → Orch 由 orchd 讀入口 thread 的 rollout；Orch → Nat 用 `codex queue`。
  這兩條都經 probe 量到逐位元組一致；Luna 重打 5 KB 會改寫成 10–13 KB（#37 handoff，`79ace31`）。
- 入口模型用 `gpt-6.1-sol`：要讓它用語音唸給 Nat 聽，轉述測試 Sol 43/43、Luna 22/40（見 docs/entry.md）。
  唸出來的版本只供收聽，批准以畫面原文為準。
- 一次只有一題 current，其餘排隊；回答以 `reply_to` 綁題，不符就拒收，不猜。
- 固定 Orch 離線時保留訊息、明說沒轉交，不啟動也不替換 Orch。
  - 修訂（#72）：Claude Code 的 daemon 會把閒置約一小時的背景 session 收掉（`retire …: idle-prompt, idle 61m`），
    這不是 Nat 停掉的。`stopped_at` 為空、job 不在或 socket 連不上時，orchd 用 `claude --bg --resume` 接回**同一個** Orch
    （同一段對話、同一個 orch_id 與綁定，job／session id 換新），再送訊息。這不是替換；`orchd orch-stop` 停掉的 Orch 永不接回，
    接回失敗時照原規則保留訊息、明說沒轉交。不做保活：daemon 只放過 attached／pinned／排程中的 session，orchd 碰不到或要每小時燒一個回合。

代價：多一層搬運、身分與恢復狀態；`role=user` 不能證明是 Nat 本人（`codex queue` 也會寫同樣的紀錄），
orchd 只排除自己送進去的文字。MCP role 只擋 orchd 自己的工具；Codex host 的其他內建工具要靠入口專案的
read-only 設定限制，這一層在 CLI 驗到有 10 個非入口工具，Native 尚未驗證。
