---
status: proposed
---

# 新 Orch 的啟動窗口與「死亡」定義（#35）

`orchd orch restart` 在 `start_orch` 成功後立刻 `adopt`。Claude 的 agents registry 在啟動初期可能已列出新 job，但還沒有 pid／status；
`owner_health` 對「有 job、pid 與 status 都空」一律回 `dead / job_retired`，`adopt` 因此拒絕剛啟動的 Orch。幾秒後同一個 Orch 就是 `alive (job_present)`。

## 決定

- **`owner_health` 不改。** `orch list`、`adopt`、`binding --status` 的語意與回傳值不變：單次快照裡 pid／status 都空仍是 `dead / job_retired`。
  單次快照分不出「還沒啟動完」和「已退場」，這個歧義由**需要等待的呼叫端**處理，不放進共用判定。
- **窗口只存在於 restart，且只在剛 `start_orch` 之後。** restart 在 adopt 前多一步 `wait-alive`（`orch_restart.wait_alive`），輪詢新 Orch 的 `owner_health`：
  - `alive`：往下做 adopt。
  - `job_retired`、`job_unverified`、`runtime_unavailable`：可能只是啟動中，每 `STARTUP_POLL_SECONDS`（1 秒）重查。
  - 其他原因（`job_absent`、`job_failed`、`job_missing`、`kind_unsupported` 等）：明確的死亡或錯誤，**不等**，立刻失敗。
- **上限 `STARTUP_WAIT_SECONDS`（30 秒）。** 到期仍不是 `alive` 就以 `step: wait-alive` 失敗，錯誤含最後一次的 state／reason 與等待秒數，
  `manual_recovery` 仍是 `orchd list; orchd adopt NEW --from OLD`。不會無限等，也不會因逾時就當作活著。
- **死亡不從啟動窗口推論。** 等到的只有 `alive`（pid 為正整數且 status 是 idle／waiting／busy）才放行；逾時視為失敗而不是成功。
  真的退場的 job 會一路維持 `job_retired`，等滿上限後被拒絕，且舊 Orch 的任務與綁定都還沒動。
- 舊 Orch 的 `confirm-dead` 不受影響：它仍用單次 `owner_health` 加 socket 探測，不套用啟動窗口。

## 不在這張票

#81 Phase 3 與 #90 也會碰到 restart／adopt 的健康判定；本決定只定義上面這一個窗口，不處理那兩張的範圍。
