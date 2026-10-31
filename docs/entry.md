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
`gpt-6.1-terra`、推理 low、自訂權限 `interface`（家目錄 deny、只讀 interface 資料夾、無網路）、只掛 orchd 的入口 MCP，
且 `relay`／`status` 預先核准（`approval_policy = "never"` 下沒核准的 MCP 工具會被直接擋掉）。
已存在且內容不同的檔案不會被覆蓋。

新增 foreground 後，既有入口需要重新產生 `~/orch/interface/.codex/config.toml` 與 `~/orch/interface/AGENTS.md`：
先將這兩個檔案移到備份位置，再用新版程式跑 `orchd init`，核對輸出為 created，必要時重新套用自己的修改，
然後重開 Desktop 入口對話以載入新設定。自訂 interface 路徑時用相應路徑。
`orchd upgrade` 現在會以 migration 更新 AGENTS.md 的 managed 規則區塊（見[家目錄更新](upgrade-migrations.md)），但不刷新 `.codex/config.toml`，也不呼叫 init；
單獨再跑 init 也會保留舊的不同內容。此變更未升級、重啟或改動任何 live 入口。

1. 在 Desktop 打開 `~/orch/interface`，權限選 **`interface`**（init 寫好的權限設定），不要用完整存取權。
2. 綁定的 Orch 閒置被 Claude 收掉時，送訊息或跑 `orchd binding` 會原地接回同一個 Orch（#72）。
   用 `orchd orch stop` 停掉、或接回失敗時不會自己替換；確定要換用 `orchd binding --new`（舊 Orch 的未答問題留在舊 Orch）。
   綁到指定的 Orch 用 `orchd binding --to <orch_id>`，`orchd binding --status` 唯讀查看。

入口模型用 `gpt-6.1-terra`、推理 low（可由 config 的 `[interface]` `model`／`effort` 覆寫，init 讀取；改自 `gpt-6.1-sol`，為降低成本；理由與未驗證處見下方「入口模型」）。未 live 驗證：Native Desktop 是否套用這份權限、
全域 `~/.codex/config.toml` 的 MCP 與工具在 interface 是否仍出現、語音模式實際用哪個模型、
orchd 的 MCP server 在 Native 是否也不受這份權限限制（它要寫 `ORCHD_HOME` 的資料庫、讀 `~/.codex/sessions`；CLI 實測不受限）。

## 工具

| 角色 | 工具 | 說明 |
| --- | --- | --- |
| 入口 | `relay(reply_to?)` | 轉交 Operator 這一則訊息；原文由 orchd 從 rollout 讀，沒有 text 參數 |
| 入口 | `status()` | 綁定、Orch 健康、目前問題全文、排隊數、未送達項目；重開時先呼叫 |
| Orch | `entry_inbox` | 讀 Operator 原文，附 sha256、`reply_to`、`task_id` |
| Orch | `send_to_nat(text)` | 一般訊息；UTF-8 超過 8000 bytes 直接報錯、不存，請分成編號訊息（1/3、2/3） |
| Orch | `resend_to_nat(message_id)` | 立刻重送自己 `failed`／`held` 的訊息（CLI：`orchd entry resend MESSAGE_ID`） |
| Orch | `ask_nat(text?, task_id?, quote_worker_question?)` | 一次一題，其餘排隊；可原樣引用 worker 的問題（對外預覽） |
| Orch | `answer(task_id, entry_reply_id=N)` | 把 Operator 的回覆原文轉給 worker；task 不符就拒絕 |

## 送達狀態（Orch → Operator）

| 狀態 | 意思 | 之後 |
| --- | --- | --- |
| `pending` | 還沒送：Desktop thread 還沒出現，或排在較早的訊息後面 | 自動重試 |
| `failed` | 送過一次但 `codex queue` 報錯 | 自動重試（入口下次呼叫時）；也可 `resend_to_nat` |
| `held` | 連續失敗 3 次（例如內容永遠送不過）：不再自動重試，**也不再擋後面的訊息** | 排除原因後 `resend_to_nat`；會排在這段期間送出的訊息之後 |
| `uncertain` | 可能已進 Desktop queue 但沒存到回執 | 不會自動重送，`resend_to_nat` 也不收（只收 failed／held）；請 Operator 確認有沒有收到，沒收到就用 `send_to_nat` 重寫一則 |
| `delivered` | 已進 Desktop queue（不代表 Operator 看過或批准） | — |

`send_to_nat` 內文上限 8000 UTF-8 bytes（`entry.MAX_SEND_BYTES`）；超過在存入前就報錯。此上限只針對 `send_to_nat`。

## 圖片附件

`relay` 保留 rollout `UserMessage.content` 的結構化 `local_image.path`，立即複製到
`/tmp/claude-<uid>/<Orch cwd key>/attachments/<entry>/<message>/`（macOS 會呈現 resolve 後的
`/private/tmp/...`），完成後才通知 Orch。uid 與 cwd key 由 `paths.orch_home()` 推導，沿用
啟動 Orch 時已允許讀取的圖片目錄；不修改 Claude settings 或 Orch 家。非法 entry 路徑字元會以 hash 編碼。
不從任意正文路徑抓檔案，也不下載 URL。

來源 allowlist 只包含 macOS `/usr/bin/getconf DARWIN_USER_TEMP_DIR` 回報的本使用者暫存根目錄下
`TemporaryItems/NSIRD_screencaptureui_<6 個英數字元>/<單一檔案>`：這是現有 Entry 實例確認的
截圖路徑。沒有證據支持更廣的 ChatGPT／Codex 暫存 pattern，因此不接受整個 `T/`、Downloads、
專案、Orch 家或 `TMPDIR` 指定的路徑。未來其他來源需要另行確認 allowlist，拒絕時會提供 reason code。
open 前檢查來源路徑，open 後用 macOS `F_GETPATH` 從 fd 取得實際位置，再確認仍在 allowlist
且與原路徑一致；不會在這些檢查之前讀取內容，也拒絕 hard link。

只複製目前使用者擁有的 regular file；以 magic bytes 辨認 PNG、JPEG、GIF、WebP、HEIC／HEIF，
副檔名必須在 `.png/.jpg/.jpeg/.gif/.webp/.heic/.heif` 清單中且與 signature／ftyp brands 一致。
`.heif` 可容納 HEIC，`.heic` 必須具有 HEIC brand；不接受 AVIF、未知或不完整的 ftyp header。
不符時不複製，只留下不含檔案內容的診斷代碼；manifest v2 才代表經過來源及格式驗證，舊 v1
快取不再由 inbox 提供可讀路徑。每張最多 20 MiB、每則最多 10 張。來源與目標的
symlink（含父目錄）會拒絕，macOS 固定的 `/var`、`/tmp` 系統別名例外。使用私有目錄 0700、
檔案及 manifest 0600、原子完成與跨程序鎖；重複 relay 不重複複製。缺檔、權限或容量問題
不丟掉 Operator 文字，`entry_inbox` 會附失敗說明，請 Operator 重傳。

SQLite 的 `body`、`source_raw` 與原文 digest 不變，也不改 schema。`entry_inbox.body` 保留原文，
再附原路徑與 Orch 可讀的新路徑；`attachments` 提供複製結果。回傳的 `body_bytes/body_sha256`
對應呈現後的 body，`source_body_bytes/source_body_sha256` 對應 DB 原文；沒有附件時維持原有格式。
`answer(entry_reply_id=...)` 仍轉送 DB 原文，不包含呈現用的附件補充。

保存期限為複製後 7 天，`relay`／`entry_inbox` 活動時最多每小時清理一次，只清理 orchd 自有的
`attachments` 子樹，不碰 Claude 的 session images。長期無呼叫時可能留超過 7 天；OS 或
重開機也可能提早清除。快取過期或遺失時不再讀來源補拷，而是明確請 Operator 重傳；原 rollout
的附件 metadata（或 Desktop 的檔案包裝文字）仍可提供缺檔提示。這不是持久的附件封存，也不
回補舊訊息的附件。未在 live Desktop／Orch 驗證讀圖；自動化測試使用合成檔案與隔離 DB。

狀態：`pending`／`held`（排隊中的題目）／`delivered`／`failed`／`uncertain`／`not_delivered`（Orch 離線，已保存）。
`delivered` 只代表送到 Orch 的 socket、Desktop queue 或工具結果，不代表 Operator 已讀，更不代表同意。
`uncertain`（送出但收據沒寫進去）不會自動重送。

## 叫出 Orch 或 worker

入口的 `foreground` 只接受 `{"target":"orch"}` 或 `{"task_id":"八碼 hex id"}`，兩者不可並用，
也不接受 command、path、socket、URL 或其他參數。Orch 預設為本入口綁定的那一個；task 必須存在且目前屬於
本入口綁定的 Orch（採用後以目前 owner 為準）。

Orch 沿用 `orchd orch attach ORCH_ID --viewer`：健康檢查、閒置／被 daemon 收掉時接回同一段對話，再 attach。
worker 沿用 `view_worker`：Claude 用 `claude attach JOB`；新的 Sol 預設 app-server，用 native remote TUI，忙碌時也能 attach；
明確指定或既有的 exec Sol 不遷移，忙碌時明確拒絕，回合之間用 `codex resume`。沒有 watch-only 模式，也沒有 takeover lease。
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
**改用 Terra（2026-10-29，Operator 要求以降成本）**：上表只測過 Sol 與 Luna，`gpt-6.1-terra` 的原文轉述準確度**尚未用同一測試驗證**；部署後請先跑一次同樣的轉述測試再依賴語音。既有入口的 live 設定在 `~/orch/interface/.codex/config.toml`（init 與 upgrade 都不會覆蓋，需手動改 `model` 或依上節流程重新產生）。

Operator → Orch 的原文由 orchd 從 rollout 讀，不經模型重打；Orch → Operator 用 `codex queue`，兩條都不受入口模型影響。


## Issue-linked local threads

`send_to_nat(text, issue_ref?)` and `ask_nat(text?, task_id?, ..., issue_ref?)`
accept one optional `github:owner/repo#N` or `jira:KEY-N` reference (positive number).
When `ask_nat` has a task and no explicit ref, it copies that task's latest issue
link, including an unlink. Operator replies copy the question's ref when saved;
plain incoming messages remain untagged. Later task-link changes do not rewrite
saved messages. Tags describe context, never approval or task completion.

The Orch MCP tool `entry_tag(message_id, issue_ref)` sets a ref, or clears it with
JSON null. Only messages belonging to the calling Orch on its current binding
can be tagged, including incoming messages. Rebinding does not grant permission
to tag a previous Orch's messages. Actual changes append a local
`entry_message_tags` audit row (old/new ref, actor, time); identical calls do
nothing. Changing a question's tag later does not rewrite existing replies.

Equivalent CLI writes require the calling Orch's `ORCHD_ORCH_ID` environment:

```sh
orchd entry send --text 'Progress update' --issue-ref github:example/project#40
orchd entry ask --text 'Which option?' --task-id TASK_ID --issue-ref jira:DEMO-37
orchd entry tag MESSAGE_ID --issue-ref github:example/project#40
orchd entry tag MESSAGE_ID --clear
```

Read summaries locally, without runtime calls, delivery, inbox consumption or
migration of the source DB:

```sh
orchd entry thread --issue-refs github:example/project#40,jira:DEMO-37 --limit 3 --json
# Single-ref alias and explicit entry selection:
orchd entry thread --issue-ref github:example/project#40 --entry desktop --json
```

The reader uses a private DB/WAL copy with `query_only=ON`. It selects only the
named entry (default `desktop`), including its historical bindings. It never
includes another entry's messages. Limit is per ref, defaults to 3, and accepts
1–100. JSON is always printed, with this versioned contract:

```json
{
  "schema_version": 1,
  "entry_id": "desktop",
  "threads": {
    "github:example/project#40": {
      "status": "waiting_nat",
      "messages": [
        {"message_id": 123, "at": 1700000000.0, "who": "orch",
         "direction": "orch→nat", "kind": "question", "text": "Which option?", "full_bytes": 13}
      ]
    },
    "jira:DEMO-37": {"status": "none", "messages": []}
  }
}
```

Messages are newest first (`created_at DESC, id DESC`); `at` is Unix seconds.
`who` is `orch` or `nat`; direction is `orch→nat` or `nat→orch`.
`text` is the first 200 Unicode code points, with no added ellipsis;
`full_bytes` counts the original body's UTF-8 bytes. Status reflects only the
latest stored speaker: Orch → `waiting_nat`, Operator → `waiting_orch`, no
messages → `none`. It does not assert delivery, an active question or approval.
Every requested ref has a result, including empty ones. Invalid refs/limits
fail with exit 1 and stderr. A DB predating `issue_ref` returns empty threads
without migrating it; ordinary store startup later adds the nullable column.

Bodies stay in the local DB and local CLI output. Do not publish this output
or put it into tracker comments or public caches. The snapshot command's body
policy is unchanged; thread summaries use this separate reader.
