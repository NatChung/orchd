# Desktop 入口（#37）

Nat 在 Codex Desktop 打字 → orchd 從該 thread 的 rollout 讀原文 → 綁定的 Claude Orch 用 `entry_inbox` 讀取；
Orch 用 `send_to_nat`／`ask_nat` 回覆 → orchd 以 `codex queue` 原樣送進同一個 Desktop thread。設計理由見
[ADR-0003](adr/0003-desktop-entry.md)。

## 設定

```sh
orchd init         # 建 ~/orch/home 與 ~/orch/interface、在 Codex trust 這兩個資料夾
orchd interface    # 沿用已綁定且在線的 Claude Orch；沒有就開一個 Opus Orch 並綁上
```

`orchd init` 寫入 interface 的 `.codex/config.toml`（範本在 `orchd/templates/interface/`）與給入口模型的 `AGENTS.md`：
`gpt-6.1-sol`、推理 low、自訂權限 `interface`（家目錄 deny、只讀 interface 資料夾、無網路）、只掛 orchd 的入口 MCP，
且 `relay`／`status` 預先核准（`approval_policy = "never"` 下沒核准的 MCP 工具會被直接擋掉）。
已存在且內容不同的檔案不會被覆蓋。

1. 在 Desktop 打開 `~/orch/interface`，權限選 **Custom (config.toml)**，不要用 Full access。
2. 綁定的 Orch 離線時 `orchd interface` 不會自己替換；確定要換用 `orchd interface --new`（舊 Orch 的未答問題留在舊 Orch）。
   手動綁定仍可用 `orchd entry-bind <orch_id>`，`orchd entry-status` 唯讀查看。

入口模型用 `gpt-6.1-sol`（理由見下方「入口模型」）。未 live 驗證：Native Desktop 是否套用這份權限、
全域 `~/.codex/config.toml` 的 MCP 與工具在 interface 是否仍出現、語音模式實際用哪個模型、
orchd 的 MCP server 在 Native 是否也不受這份權限限制（它要寫 `ORCHD_HOME` 的資料庫、讀 `~/.codex/sessions`；CLI 實測不受限）。

## 工具

| 角色 | 工具 | 說明 |
| --- | --- | --- |
| 入口 | `relay(reply_to?)` | 轉交 Nat 這一則訊息；原文由 orchd 從 rollout 讀，沒有 text 參數 |
| 入口 | `status()` | 綁定、Orch 健康、目前問題全文、排隊數、未送達項目；重開時先呼叫 |
| Orch | `entry_inbox` | 讀 Nat 原文，附 sha256、`reply_to`、`task_id` |
| Orch | `send_to_nat(text)` | 一般訊息 |
| Orch | `ask_nat(text?, task_id?, quote_worker_question?)` | 一次一題，其餘排隊；可原樣引用 worker 的問題（對外預覽） |
| Orch | `answer(task_id, entry_reply_id=N)` | 把 Nat 的回覆原文轉給 worker；task 不符就拒絕 |

狀態：`pending`／`held`（排隊中的題目）／`delivered`／`failed`／`uncertain`／`not_delivered`（Orch 離線，已保存）。
`delivered` 只代表送到 Orch 的 socket、Desktop queue 或工具結果，不代表 Nat 已讀，更不代表同意。
`uncertain`（送出但收據沒寫進去）不會自動重送。

## 已知限制與未驗證

- `role=user` 不能證明是 Nat：其他程式用 `codex queue` 寫進這個 thread 的文字，orchd 分不出來；只排除 orchd 自己送的。
- 沒拿到 turn id 時取該 thread 最新一則 user 訊息；`_meta` 有哪些 key 會寫到 MCP stderr（`orchd entry meta keys`），尚未確認 Desktop 是否帶 turn id。
- 同一 turn 內 rollout 是否已寫入 Nat 的訊息未驗證；沒寫入時回 `source_not_ready`，不會拿舊訊息代替。
- 同時開兩個入口對話時，Orch 的回覆送往最後呼叫的那一個。
- Codex 內建工具（#55，2026-10-04 用 `codex exec` 在 `~/orch/interface` 實測）：未關閉時 web search、shell、產圖、goal
  都能用，而且 web search 不受 `network.enabled = false` 限制。範本已用 `web_search = "disabled"` 與 `[features]`
  關掉 web、shell、產圖、看圖、goal、apps、memories、browser／computer use；改檔與讀家目錄由權限擋住。
  子代理關不掉（`multi_agent = false`、`agents.max_depth = 0` 都無效，`max_concurrent_threads_per_session` 最小是 1），
  但子代理沿用同一個模型與權限設定。interface 也會載入全域的 `~/.codex/AGENTS.md` 與 skills 清單。
- 未 live 驗證：Native 畫面能否完整呈現原文、MCP reload、Q1–Q3 端到端、worker → Opus → Desktop → Opus → worker 完整迴圈。

## 入口模型（2026-10-03 CLI 測試）

同一段 5.5 KB 中英混合文字（含 `\r\n`、tab、引號、code block），請模型「原文完整轉述」，`codex exec` 每次新對話。
只差標點、空白、換行算 Pass；文字、數字少一個或多一個就 Fail。

| 模型 | Pass | 失敗型態 |
| --- | --- | --- |
| `gpt-6.1-sol`（medium） | 43 / 43 | 無；每次只有 `\r\n` 變 `\n` |
| `gpt-6.1-sol`（low） | 20 / 20 | 同上 |
| `gpt-6-luna` | 22 / 40 | 掉最後一行、整段約 1300 字截斷、多重複字詞 |

加上 `AGENTS.md`「一字不改重新輸出」的 prompt 沒有改善 Luna。依官方價目，比 Luna 準的選項裡 `gpt-6.1-sol`
最便宜（$2 / $0.10 快取 / $10 輸出，每 1M token）。入口模型每次收到 Orch 訊息都把內文完整重講一次（文字與語音都一樣；只寫「語音時才唸」時，文字模式下它會不出聲），這份只供收聽；批准以畫面上 orchd 送來的原文為準。
Nat → Orch 的原文由 orchd 從 rollout 讀，不經模型重打；Orch → Nat 用 `codex queue`，兩條都不受入口模型影響。
