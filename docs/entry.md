# Desktop 入口（#37）

Operator 在 Codex Desktop 打字 → orchd 從該 thread 的 rollout 讀原文 → 綁定的 Claude Orch 用 `entry_inbox` 讀取；
Orch 用 `send_to_nat`／`ask_nat` 回覆 → orchd 以 `codex queue` 原樣送進同一個 Desktop thread。設計理由見
[ADR-0003](adr/0003-desktop-entry.md)。

## 設定

```sh
orchd init         # 建 ~/orch/home 與 ~/orch/interface、在 Codex trust 這兩個資料夾
orchd binding      # 沿用已綁定且在線的 Claude Orch；沒有就開一個 Opus Orch 並綁上
```

`orchd init` 寫入 interface 的 `.codex/config.toml`（範本在 `orchd/templates/interface/`）與給入口模型的 `AGENTS.md`：
`gpt-6.1-sol`、推理 low、自訂權限 `interface`（家目錄 deny、只讀 interface 資料夾、無網路）、只掛 orchd 的入口 MCP，
且 `relay`／`status` 預先核准（`approval_policy = "never"` 下沒核准的 MCP 工具會被直接擋掉）。
已存在且內容不同的檔案不會被覆蓋。

新增 foreground 後，既有入口需要重新產生 `~/orch/interface/.codex/config.toml` 與 `~/orch/interface/AGENTS.md`：
先將這兩個檔案移到備份位置，再用新版程式跑 `orchd init`，核對輸出為 created，必要時重新套用自己的修改，
然後重開 Desktop 入口對話以載入新設定。自訂 interface 路徑時用相應路徑。
`orchd upgrade` **不會**自動做這一步：它只重新安裝程式，不呼叫 init、不改 `~/orch`；
單獨再跑 init 也會保留舊的不同內容。此變更未升級、重啟或改動任何 live 入口。

1. 在 Desktop 打開 `~/orch/interface`，權限選 **`interface`**（init 寫好的權限設定），不要用完整存取權。
2. 綁定的 Orch 閒置被 Claude 收掉時，送訊息或跑 `orchd binding` 會原地接回同一個 Orch（#72）。
   用 `orchd orch-stop` 停掉、或接回失敗時不會自己替換；確定要換用 `orchd binding --new`（舊 Orch 的未答問題留在舊 Orch）。
   綁到指定的 Orch 用 `orchd binding --to <orch_id>`，`orchd binding --status` 唯讀查看。

入口模型用 `gpt-6.1-sol`（理由見下方「入口模型」）。未 live 驗證：Native Desktop 是否套用這份權限、
全域 `~/.codex/config.toml` 的 MCP 與工具在 interface 是否仍出現、語音模式實際用哪個模型、
orchd 的 MCP server 在 Native 是否也不受這份權限限制（它要寫 `ORCHD_HOME` 的資料庫、讀 `~/.codex/sessions`；CLI 實測不受限）。

## 工具

| 角色 | 工具 | 說明 |
| --- | --- | --- |
| 入口 | `relay(reply_to?)` | 轉交 Operator 這一則訊息；原文由 orchd 從 rollout 讀，沒有 text 參數 |
| 入口 | `status()` | 綁定、Orch 健康、目前問題全文、排隊數、未送達項目；重開時先呼叫 |
| Orch | `entry_inbox` | 讀 Operator 原文，附 sha256、`reply_to`、`task_id` |
| Orch | `send_to_nat(text)` | 一般訊息 |
| Orch | `ask_nat(text?, task_id?, quote_worker_question?)` | 一次一題，其餘排隊；可原樣引用 worker 的問題（對外預覽） |
| Orch | `answer(task_id, entry_reply_id=N)` | 把 Operator 的回覆原文轉給 worker；task 不符就拒絕 |

狀態：`pending`／`held`（排隊中的題目）／`delivered`／`failed`／`uncertain`／`not_delivered`（Orch 離線，已保存）。
`delivered` 只代表送到 Orch 的 socket、Desktop queue 或工具結果，不代表 Operator 已讀，更不代表同意。
`uncertain`（送出但收據沒寫進去）不會自動重送。

## 叫出 Orch 或 worker

入口的 `foreground` 只接受 `{"target":"orch"}` 或 `{"task_id":"八碼 hex id"}`，兩者不可並用，
也不接受 command、path、socket、URL 或其他參數。Orch 預設為本入口綁定的那一個；task 必須存在且目前屬於
本入口綁定的 Orch（採用後以目前 owner 為準）。

Orch 沿用 `orchd attach ORCH_ID --viewer`：健康檢查、閒置／被 daemon 收掉時接回同一段對話，再 attach。
worker 沿用 `view_worker`：Claude 用 `claude attach JOB`；app-server Sol 用 native remote TUI，忙碌時也能 attach；
exec Sol 忙碌時明確拒絕，回合之間用 `codex resume`。沒有 watch-only 模式，也沒有 takeover lease。
每次呼叫都要求開新的 Ghostty 視窗；重用或 focus 既有視窗不在範圍內。

成功結果包含 `target`、`orch_id`、`kind`、`launched`（viewer 與 job/session identity）、
`launch_status: "launch_requested"`、`window_opened: null`。啟動命令成功只代表已要求開窗，沒有驗證視窗可見或 OS focus；
失敗以 MCP error 回傳，不會宣稱已開窗。GUI 未 live 驗證。

## 已知限制與未驗證

- `role=user` 不能證明是 Operator：其他程式用 `codex queue` 寫進這個 thread 的文字，orchd 分不出來；只排除 orchd 自己送的。
- 沒拿到 turn id 時取該 thread 最新一則 user 訊息；`_meta` 有哪些 key 會寫到 MCP stderr（`orchd entry meta keys`），尚未確認 Desktop 是否帶 turn id。
- 同一 turn 內 rollout 是否已寫入 Operator 的訊息未驗證；沒寫入時回 `source_not_ready`，不會拿舊訊息代替。
- 同時開兩個入口對話時，Orch 的回覆送往最後呼叫的那一個。
- Codex 內建工具（#55，2026-10-04 用 `codex exec` 在 `~/orch/interface` 實測）：未關閉時 web search、shell、產圖、goal
  都能用，而且 web search 不受 `network.enabled = false` 限制。範本已用 `web_search = "disabled"` 與 `[features]`
  關掉 web、shell、產圖、看圖、goal、apps、memories、browser／computer use；改檔與讀家目錄由權限擋住。
  子代理關不掉（`multi_agent = false`、`agents.max_depth = 0` 都無效，`max_concurrent_threads_per_session` 最小是 1），
  但子代理沿用同一個模型與權限設定。interface 也會載入全域的 `~/.codex/AGENTS.md` 與 skills 清單。
- 未 live 驗證：Native 畫面能否完整呈現原文、MCP reload、Q1–Q3 端到端、worker → Opus → Desktop → Opus → worker 完整迴圈。

## 語音模式（#64，2026-10-04 實測）

Desktop 開語音時，聽與說的是即時語音模型，再交給 interface（Sol）處理。Sol 收到的使用者訊息是 `<realtime_delegation>`
包裝：`<input>` 是語音模型整理的要求，`<transcript_delta>` 是逐字稿，而且會重複前幾輪。

- **轉給 Orch 的內容**：`[語音輸入，可能有辨識錯字]` ＋ `<input>` ＋ 這一輪新說的話（最後一句 `assistant:` 之後的 `user:` 行，
  跟 `<input>` 相同就不重複）。包裝原文存在 `source_raw`，sha256 算的是轉出去的內文。
- **語音結束的交接**（`<source>transcript_tail_flush</source>`）：不是 Operator 的新要求，存成 `kind=handoff`、`delivery=skipped`，
  不通知 Orch，`relay` 回 `skipped_handoff`。之前它會被當成 Operator 的話轉給 Orch，裡面重複的逐字稿可能讓 Orch 再做一次。
- **講話中途重送**：Operator 還在講時，語音模型會在同一個 turn 裡連續送出越來越長的同一句話。同一個 turn 裡已經轉過的開頭會去掉，
  只轉新增的部分，標「（接續上一則）」；完全沒有新內容就存成 `kind=voice_repeat`、不通知 Orch。只比對同一個 turn，
  所以之後另一輪說「寄信給 Ann」不會因為前一輪說過「寄信」而被截掉。turn 記在 `source_turn`。
- **一句話被拆到兩個 turn**：語音模型也會把同一段話拆成兩個 turn，後一個的逐字稿重複前一個已轉過的句子。
  同一個對話裡 10 分鐘內已經轉過、一字不差的逐字稿句子不再轉；只有完全相同才去掉，開頭相同的新句子照轉。
- 語音模型自己回應、沒交給 interface 的話（例如一句確認），Orch 不會收到；這是語音模型的判斷，orchd 看不到。
- **辨識錯字**：例如 Orch 被聽成「O區」「O2CH」。Orch 的指示是看不懂或不確定時先問，不要猜。
- **唸給 Operator 的版本**是即時語音模型改寫過的，例如不會把 `o96e76b5` 整串唸出來。只供收聽，批准以畫面上 orchd 送來的原文為準。

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
Operator → Orch 的原文由 orchd 從 rollout 讀，不經模型重打；Orch → Operator 用 `codex queue`，兩條都不受入口模型影響。
