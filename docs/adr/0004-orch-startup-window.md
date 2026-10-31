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
- **上限 `STARTUP_WAIT_SECONDS`（30 秒）是整段 wait-alive 的硬期限，含每次探測的時間。** 每次 `live_jobs` 探測的 timeout 取
  `min(剩餘預算, 15 秒)`（15 秒是 `claude agents --json` 單次上限），sleep 也夾在剩餘時間內；探測前檢查期限，
  探測回來後再檢查一次——期限過後才回來的答案，即使是 `alive` 也不算。因此最壞情況約 30 秒加一次探測結束的誤差，不會因為慢查詢拖到 44 秒以上。
  到期仍不是 `alive` 就以 `step: wait-alive` 失敗，錯誤含最後一次的 state／reason 與等待秒數，
  `manual_recovery` 仍是 `orchd list; orchd adopt NEW --from OLD`。不會無限等，也不會因逾時就當作活著。
- **死亡不從啟動窗口推論。** 等到的只有 `alive`（pid 為正整數且 status 是 idle／waiting／busy）才放行；逾時視為失敗而不是成功。
  真的退場的 job 會一路維持 `job_retired`，等滿上限後被拒絕，且舊 Orch 的任務與綁定都還沒動。
- 舊 Orch 的 `confirm-dead` 不受影響：它仍用單次 `owner_health` 加 socket 探測，不套用啟動窗口。

## 重跑不是冪等的

wait-alive 逾時時，替換後的 Orch 可能只是還沒發布 pid/status，**仍然在跑**，舊 Orch 已停、任務與綁定還沒搬。
這時再跑一次 `orchd orch restart` 會停掉舊 Orch 的紀錄（已死）並再啟動第三個 Orch，造成兩個替換 Orch 並存；本決定**不實作**冪等重跑。
手動恢復（在可信任的本機 Operator shell 執行；以下 OLD_ID／NEW_ID 使用失敗結果中的 ID）：

1. `orchd orch list --all` 確認 NEW_ID 已 `alive`，OLD_ID 已停止。狀態不明就先停在這裡；若確定 NEW_ID 沒有在跑，才 `orchd orch stop NEW_ID` 再重新 restart。
2. 先 `orchd binding --status` 記錄 Desktop 綁定，並確認 restart 前是否指向 OLD_ID。`orchd interface list` 檢查所有註冊（含 disabled）與 pending 數量。
   沒有 interface 就跳過 rebind；有註冊時必須**全部**屬於 OLD_ID，才可執行下列兩步。若混有其他 owner，停止並人工釐清，不要強制搬走別人的註冊。
3. `orchd list`、`orchd intake backlog --owner OLD_ID` 檢查開放任務與 received receipts；有任一項才執行 `orchd adopt NEW_ID --from OLD_ID`，兩者都沒有就跳過。
   檢查 adopt 的 `committed` 與通知錯誤；已提交但通知失敗時先讀回 ownership，不要當作未搬移而盲目重跑。
4. 有 interface 且符合第 2 步時，先預覽，再提交：

   ```sh
   orchd interface rebind --from OLD_ID --to NEW_ID --dry-run
   orchd interface rebind --from OLD_ID --to NEW_ID
   ```

   dry-run 必須成功且 owner／數量符合預期才提交；提交時會再次檢查全部註冊的來源 owner。這一步搬移 interface 註冊與 pending requests，保留 token、reply route 與 ack 狀態，並喚醒 NEW_ID。
   檢查 `committed: true`、`interface_wakes` 各值為 null、沒有 `wake_error`。通知失敗不代表 ownership 回滾；先讀回並排除通知問題，讓 daemon 的持久化重試補送，不要反向搬移。
5. 再 `orchd binding --status`：只有 Desktop **原本就指向 OLD_ID，且期間沒被其他操作更改**時，才 `orchd binding --to NEW_ID`。
   恢復期間協調其他 Operator 不要更改 binding；CLI 沒有 compare-and-swap，單次讀回不能防止同時寫入。原本未綁 OLD_ID 或已被更改就保留現況。
6. `orchd list`、`orchd intake backlog --owner NEW_ID`、`orchd interface list`、`orchd binding --status` 讀回，確認任務／received receipts／interfaces 與預期 Desktop 都屬於 NEW_ID。
   在 NEW_ID 的 Orch 用 `interface_requests` 讀回 pending request ID（超過一頁要分頁），確認收到 interface wake。`binding` 本身不會移轉 interfaces／pending requests，不能取代第 4 步。

## 不在這張票

#81 Phase 3 與 #90 也會碰到 restart／adopt 的健康判定；本決定只定義上面這一個窗口，不處理那兩張的範圍。
