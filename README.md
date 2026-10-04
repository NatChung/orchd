# orchd

由 Orch 派工、worker 執行，讓多件工作並行且可管理。名詞定義見 [CONTEXT.md](CONTEXT.md)。

- **Orch**：只拆工作、派工、核對回報的 session（Claude Opus，或 Codex Desktop thread）；不讀寫專案 repo。
- **Worker**：為單一任務在獨立 worktree 裡工作並回報；預設 `gpt-6.1-sol`，換廠商用 `claude-sonnet-5-5`。
- **interface**：Nat 在 Codex Desktop 交辦、回答追問的入口，原文轉交給一個固定的 Claude Orch（[docs/entry.md](docs/entry.md)）。

狀態存在 `~/.local/share/orchd/orchd.db`（`ORCHD_HOME` 可改）。

## 安裝

不用 clone repo（需要 Python 3.11+，uv 會自己準備）：

```sh
uv tool install git+ssh://git@github.com/NatChung/orchd
orchd doctor
```

- **多個 GitHub 帳號時**：`git@github.com` 用的是機器預設的 SSH key，沒權限會出現 `Repository not found`。改用 `~/.ssh/config` 裡有權限那個帳號的 Host 別名，例如 Nat 的機器：
  `uv tool install git+ssh://git@github-NatChung/NatChung/orchd`
- repo 目前是 private：要有讀取權限；HTTPS 安裝需要 token，所以用 SSH。
- `orchd: command not found`：`~/.local/bin` 不在 PATH，跑一次 `uv tool update-shell` 再開新終端機。
- `orchd doctor` 在 `orchd init` 之前會顯示「orch home not set up yet」，這是提醒下一步，不是失敗。
- MCP 不另外安裝：就是同一支程式的 `orchd mcp`，由 `orchd init`（和 `orchd orch`）寫進設定，設定裡記的是安裝後 `orchd` 的絕對路徑。
- 在 repo 裡開發時直接跑 `bin/orchd`；這時產生的設定會指向這份 checkout。

### 升級

```sh
orchd upgrade
```

- 從 uv 的安裝紀錄讀出當初的來源（含 SSH 別名），重新安裝最新的 commit，並顯示 `舊 commit -> 新 commit`；已是最新就不重裝。
- 當初裝的是某個 branch（`@<branch>`）時，會改回預設 branch 並說明。
- 只換程式，不動 `~/orch`、資料庫與 `~/.config/orchd`。已經在跑的 Orch 和 Desktop 的 MCP 還是舊程式：用 `orchd binding --new` 換新的 Orch，Desktop 開新對話。
- 還沒有 `orchd upgrade` 的舊版本，先手動裝一次（整行一起貼）：`uv tool install --force --refresh git+ssh://git@github.com/NatChung/orchd`（有用 SSH 別名就用別名）。`uv tool upgrade orchd` 會沿用快取、抓不到新 commit，不要用。
- 先試某個 branch：`uv tool install --force --refresh git+ssh://…/orchd@<branch>`，測完 `orchd upgrade` 就會回到預設 branch。
- 從 repo checkout 跑的 `bin/orchd`：`orchd upgrade` 會提醒你在 repo 裡 `git pull`。

### 完整移除後重裝

```sh
orchd orch-stop <orch_id>        # 先停掉在跑的 Orch（orchd orchs 看得到）
uv tool uninstall orchd
```

再視需要刪除：`~/.local/share/orchd`（資料庫：任務歷史與統計，**刪了不能復原，先備份**）、`~/orch`、
`~/.codex/config.toml` 裡 `~/orch/home`、`~/orch/interface` 兩筆 `[projects]` trust。`~/.config/orchd` 是個人設定，通常保留。
重裝照上面「安裝」與下面「開始用」。

### 個人設定（不進 repo）

放在 `~/.config/orchd/`（`ORCHD_CONFIG_DIR` 可改）：

- `init.toml`：Orch 家的 Codex 權限額外要讀的路徑、要關掉的 app。

  ```toml
  [home]
  read = ["~/AGENTS.md", "~/.codex/guidance"]
  disabled_apps = ["connector_xxx"]
  ```
- `gh-accounts.json`：worker 的 gh 依 repo owner 選帳號，例如 `{"NatChung": "NatChung", "*": "ariontechs"}`。沒有這個檔時 gh 照原本的登入狀態執行。

## 開始用

```sh
orchd init                          # 第一次：建 ~/orch/home、~/orch/interface，並在 Codex trust
orchd init --from ~/projects/orch   # 從舊 Orch 家搬檔案（不覆蓋、不刪來源）
orchd binding                       # 綁定在線的 Claude Orch，沒有就開一個
```

`orchd init` 建出：

```
~/orch/
  home/        Orch 家：AGENTS.md、PROJECTS.md、handoffs/、groups/、.codex/config.toml
  interface/   Desktop 入口：AGENTS.md、.codex/config.toml（gpt-6.1-sol、唯讀、只掛入口 MCP）
```

- 範本在 `orchd/templates/`；已存在且被改過的檔案不會被覆蓋。`ORCHD_ROOT`、`ORCHD_ORCH_HOME`、`ORCHD_INTERFACE_HOME` 可改位置。
- Codex trust 由 init 寫入 `~/.codex/config.toml`（先備份）。Claude trust 只檢查：沒 trust 時 init 會告訴你在 `~/orch/home` 開一次 `claude` 接受。
- `orchd binding`：沿用已綁定且在線的 Orch，沒有就開一個 Claude Opus Orch 並綁上；綁定會保存，重開 Desktop 不用重跑。
- Orch 閒置約一小時會被 Claude 收掉；之後有訊息要送給它、或跑 `orchd binding` 時，orchd 會接回同一個 Orch（同一段對話與綁定，#72）。
- 用 `orchd orch-stop` 停掉、或接回失敗的 Orch 不會自己換；確定要換用 `orchd binding --new`，要綁到某個已在跑的 Orch 用 `orchd binding --to ORCH_ID`，只想看狀態用 `orchd binding --status`。

之後在 Codex Desktop 打開 `~/orch/interface`，權限選 **`interface`**（init 寫好的權限設定；不要選完整存取權），開新對話直接講話：

- 對話開始先說「呼叫 status」：它會說綁定哪個 Orch、是否在線，有沒回答的問題就唸出來。
- 你說話後它轉給 Orch，只回一句短的「好，我想一下」；轉交失敗才會說原因。
- Orch 的回覆會原文出現在對話裡，它再完整講一次（語音模式會唸出來）。要你批准的事以畫面上的原文為準。

另外兩種 Orch 照舊：`orchd orch` 開 Claude Opus Orch（`--no-attach` 只開在背景）；Codex Desktop 在 `~/orch/home` 開的 session 也是一個 Orch，自成一組（[ADR-0002](docs/adr/0002-multiple-groups.md)）。

## 指令

| 指令 | 用途 |
| --- | --- |
| `orchd init [--from OLD] [--no-trust]` | 建 `~/orch/home` 與 `~/orch/interface`，在 Codex trust |
| `orchd binding` | 把 interface 綁定在線的 Claude Orch（沒有就開一個） |
| `orchd binding --new` | 開一個新的 Claude Orch 並綁上 |
| `orchd binding --to ORCH_ID` | 綁到指定、在線的 Claude Orch |
| `orchd binding --status` | 唯讀：綁定、Orch 健康、問題與交付狀態 |
| `orchd upgrade` | 安裝版更新到最新 commit（只換程式） |
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
