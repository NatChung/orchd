# orchd

由 Orch 派工、worker 執行，讓多件工作並行且可管理。名詞定義見 [CONTEXT.md](CONTEXT.md)。

- **Orch**：只拆工作、派工、核對回報的 session（Claude Opus，或 Codex Desktop thread）；不讀寫專案 repo。
- **Worker**：為單一任務在獨立 worktree 裡工作並回報；預設 `gpt-6.1-sol`，換廠商用 `claude-sonnet-5-5`。
- **interface**：Nat 在 Codex Desktop 交辦、回答追問的入口，原文轉交給一個固定的 Claude Orch（[docs/entry.md](docs/entry.md)）。

狀態存在 `~/.local/share/orchd/orchd.db`（`ORCHD_HOME` 可改）。

## 開始用

```sh
orchd init                          # 第一次：建 ~/orch/home、~/orch/interface，並在 Codex trust
orchd init --from ~/projects/orch   # 從舊 Orch 家搬檔案（不覆蓋、不刪來源）
orchd interface                     # 綁定在線的 Claude Orch，沒有就開一個
```

`orchd init` 建出：

```
~/orch/
  home/        Orch 家：AGENTS.md、PROJECTS.md、handoffs/、groups/、.codex/config.toml
  interface/   Desktop 入口：AGENTS.md、.codex/config.toml（gpt-6.1-sol、唯讀、只掛入口 MCP）
```

- 範本在 `orchd/templates/`；已存在且被改過的檔案不會被覆蓋。`ORCHD_ROOT`、`ORCHD_ORCH_HOME`、`ORCHD_INTERFACE_HOME` 可改位置。
- Codex trust 由 init 寫入 `~/.codex/config.toml`（先備份）。Claude trust 只檢查：沒 trust 時 init 會告訴你在 `~/orch/home` 開一次 `claude` 接受。
- 綁定的 Orch 離線時，`orchd interface` 不會自己換；確定要換用 `orchd interface --new`。

之後在 Codex Desktop 打開 `~/orch/interface`，權限選 **Custom (config.toml)**，直接講話。

另外兩種 Orch 照舊：`orchd orch` 開 Claude Opus Orch（`--no-attach` 只開在背景）；Codex Desktop 在 `~/orch/home` 開的 session 也是一個 Orch，自成一組（[ADR-0002](docs/adr/0002-multiple-groups.md)）。

## 指令

| 指令 | 用途 |
| --- | --- |
| `orchd init [--from OLD] [--no-trust]` | 建 `~/orch/home` 與 `~/orch/interface`，在 Codex trust |
| `orchd interface [--new]` | 把 interface 綁定在線的 Claude Orch（沒有就開一個） |
| `orchd entry-status [--entry NAME]` | 唯讀：interface 綁定、Orch 健康、問題與交付狀態 |
| `orchd entry-bind ORCH_ID [--entry NAME] [--force]` | 手動把 interface 綁到指定 Claude Orch |
| `orchd orch [--model opus\|sonnet] [--no-attach]` | 開 Claude Orch |
| `orchd orch-stop ID` | 停 Claude Orch |
| `orchd orchs` | 唯讀 Orch 清單 |
| `orchd list` | 未結任務、worker／Orch 健康、通知失敗 |
| `orchd watch [--since HH:MM]` | 即時看 Orch 與 worker 的訊息 |
| `orchd summary [--since HH:MM]` | 每個 Orch 的 worker、模型、問題、token |
| `orchd stats [--since T] [--json]` | 每個 Orch × 任務類型的數量、重工、token、成本估算 |
| `orchd doctor [--profile nat] [--json]` | 唯讀檢查本機設定 |
| `orchd adopt NEW_ORCH [TASK_ID...] [--from OLD] [--force]` | 把未結任務移給另一個 Orch |
| `orchd close ID` | 停 worker，安全時清掉 worktree |
| `orchd mcp [--role orch\|entry] [--entry NAME]` | MCP server（Orch 或 interface 用，通常不用手動跑） |

Worker 用：`orchd ack`、`orchd progress`、`orchd ask`、`orchd report`、`orchd verify`（見 `orchd --help`）。

## 文件

- [docs/entry.md](docs/entry.md)：interface 的工具、交付狀態、入口模型測試與未驗證項目
- [docs/decisions.md](docs/decisions.md)：系統決策紀錄
- [docs/adr/](docs/adr/)：架構決策
- [docs/orch-permissions.md](docs/orch-permissions.md)：Claude Orch 的檔案權限
- [docs/review-merge-policy.md](docs/review-merge-policy.md)：worker 的 review／merge 規則

## 測試

```sh
python3 -m unittest discover -s tests
```
