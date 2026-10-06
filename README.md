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

從 `orchd orchs` 找到 id 後，用 `orchd attach <id>` 接回同一個 Claude Orch。
存活必須有 runtime 的 pid 與已知 live status；歷史 job 的 outcome 不能證明存活。
Claude daemon 閒置退出，但仍保留 session、job 與 socket 身分且未經 `orch-stop`，顯示為 `idle (session_resumable)`。
attach 會明示恢復原對話，沿用 #72 的 `--resume` 流程（原 Orch id 不變，runtime job/session id 可更新），再驗證後 attach。
查詢失敗、身分衝突或目標死亡會報錯；不改 Desktop binding，也不停止其他 session。
Codex 沒有可靠的存活探測，維持 unknown；預設僅提示 unknown 數量，詳情用 `--all`。

預設清單中的死 Orch，只在仍有未結任務、通知／問題或 Desktop binding 時合併成一行接管提醒。
CLI 查詢及 MCP `list_orchs` 會記錄成功的死亡探測：第一次與後續確認至少隔一小時，且沒有任何上述掛件，才封存。
查詢失敗或不確定會中斷死亡觀察窗口；Codex unknown 永不自動封存。
封存只是一個可還原標記，不刪任務、訊息或統計，不改 `stopped_at`。
用 `--restore ID` 解除標記；之後探測到 alive／idle 也會自動解除。
死 Orch 手動還原後仍需 `--all` 才能查看，且會重新開始一小時觀察。
MCP `list_orchs` 的文字內容維持完整 JSON（包含 dead、unknown、archived 與觀察時間），並同時附上相同的 `structuredContent`；精簡顯示只用於 CLI。不能把 unknown 當成死亡。


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
| `orchd orchs [--all]` | 預設只列 alive／idle 可接回的 Orch；`--all` 包含 dead、unknown、封存 |
| `orchd orchs --restore ID` | 解除封存並重設死亡觀察窗口，保留所有歷史 |
| `orchd attach ID [--viewer]` | 接回指定 Claude Orch；`--viewer` 用 Ghostty 開窗 |
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

## 協作

- 改動一律開 branch、推上來開 PR，由 Nat review 後 merge。不要直接推 `main`，也不要自己 merge。
- 理由：`main` 沒有 branch protection（私有 repo、免費方案不支援），推上 `main` 的東西不會被擋；而 `orchd upgrade` 裝的就是預設 branch 最新的 commit，大家下次升級就會裝到。
- 想先在自己機器上試 branch：`uv tool install --force --refresh git+ssh://…/orchd@<branch>`，試完 `orchd upgrade` 回到 `main`。

## 測試

```sh
python3 -m unittest discover -s tests
```

## Opt-in Codex app-server workers

App-server workers require `websocket-client`. Reinstall with dependencies after a code-only
checkout update (`uv pip install -e .` in its environment); installed tools should use
`orchd upgrade`. Default exec workers do not import or require websocket-client.

`dispatch(..., model="sol", backend="app-server")` selects a private app-server and RPC
supervisor for that task attempt. Omitting `backend` keeps `exec`; existing rows migrate to
`exec`, and Claude workers keep their existing transport. This does not enable the shared
Codex daemon or change global Codex configuration. Tested with `codex-cli 0.160.1`;
[the upstream app-server protocol](https://learn.chatgpt.com/docs/app-server) is experimental.

Each attempt stores its generation, supervisor/server PID + birth time + process group,
private UDS endpoint (0700 directory, 0600 sockets), thread and active turn. `socket` remains
the Claude transport field. `view_worker(task_id)` opens a new Ghostty window running the
native `codex --remote unix://ENDPOINT resume THREAD` through a registered wrapper, while
busy or idle. Closing the window releases the viewer without stopping the worker. Multiple
viewers can attach; normal FIFO answers wait until all viewers exit to protect human drafts.
An explicit interrupt cancels the matching turn, waits for `interrupted`, then sends the
correction and pending FIFO as a new turn, including when a viewer is attached.

`turn/completed` drives a generation/thread/turn-guarded flush. RPC deliveries are journaled
before sending; a confirmed response commits queued-message receipts. A disconnect or receipt
failure leaves pending visible and blocks automatic resend. Reconnection resumes the same
thread and reads paginated full turn history; matching user-message `clientId` reconciles
receipts. Missing history evidence stays uncertain: inspect `worker_deliveries` and the task's
private logs before an explicit retry. This is not an exactly-once guarantee across RPC and
SQLite. Retry stops the old attempt before replacing it; retrying to Sonnet returns to its
existing backend. Close stops registered viewers, supervisor, server and marked descendants,
using bounded TERM/KILL and stored process identities. Stop failure preserves worktree/pending.
`worker_alive` is `alive`, `idle`, `active`, `unknown` or `dead` for app-server tasks; legacy
workers retain their existing boolean/null values. Report/ask status is separate from turn state.
Opt-in tasks require lifecycle clients running this version; the existing installed service is not
upgraded by these changes. The native TUI can change its model/settings; task model bookkeeping
is not reconciled from those UI changes (structured FIFO turns reapply the task model/permissions).

Manual isolated acceptance (requires tmux, Codex authentication, and installed dependencies):

```sh
python3 scripts/app_server_e2e.py
```

The script uses a temp git worktree, private DB/CODEX_HOME/tmux socket, and temporary auth copy
removed in `finally`. It verifies mid-turn attach and same-turn input, viewer-close continuation,
one queued-answer auto-flush, and close with no leftover processes. Unit tests additionally
cover stale guards, uncertain receipts, reconnect reconciliation, interrupt ordering, failed
startup, ownership, and targeted process-group kill. No Ghostty GUI/window-focus test, long-run
resource measurements, or full worker MCP/browser integration test has been performed here.
The supervisor does not restart a crashed server or replay uncertain deliveries automatically;
its process identity and pending state remain available for explicit retry/close. Private runtime
logs/directories are retained for diagnosis. Native interrupt cancels Codex-owned tools; detached
marked jobs are guaranteed cleanup on retry/close, not on interrupt.

The later Desktop foreground tool should accept only a task id (and optionally expected
generation), check its binding/ownership, then call this existing viewer path. It must not accept
commands, paths or endpoints, must report unknown/dead/superseded attempts, and must distinguish
opening a new window from focusing an existing one. This PR does not add that entry tool.
