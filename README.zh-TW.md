# orchd

[English](README.md)

orchd 管理多個 AI coding agent 任務：Orch 拆解工作並派工，worker 在獨立 Git worktree 執行單一任務，透過 ack、progress、ask、report 回報。任務、問題、交付與目標保存在本機 SQLite；worker 回報完成後仍需核對證據。

## 需求

- macOS；Python 3.11+、Git、[uv](https://docs.astral.sh/uv/)。
- [Claude Code](https://claude.com/claude-code)：已登入並支援背景 session；目前 transport 依賴 `--bg` 與實驗性的 messaging socket，CLI 更新可能影響相容性。
- [Codex CLI](https://github.com/openai/codex)：使用 Codex worker 時需要登入；Codex Desktop 為選用的對話入口。
- tmux：背景 session；Ghostty：選用的視窗 attach／viewer。GitHub CLI：選用的 tracker 與帳號路由。
- 安裝依賴包含 psutil 與 websocket-client。app-server worker 為選用功能，預設 worker 使用 exec。

先完成各供應商登入，再對自己的專案與 Orch home 接受 trust 提示。憑證保留在各 CLI 的登入設定。

## 安裝與更新

```sh
uv tool install git+https://github.com/NatChung/orchd.git
uv tool update-shell
orchd doctor
```

`doctor` 是唯讀檢查，尚未 init 時會提醒建立 Orch home。從 checkout 開發可使用 `bin/orchd`。

```sh
orchd upgrade
```

upgrade 讀取 uv 安裝紀錄的來源，安裝該來源預設 branch 的最新 commit；branch pin 會移除。它不更新資料庫或個人設定。既有 session／MCP process 仍持有原本程式，請在方便時開始新 session；更新不是重新啟動 daemon 的指令。Checkout 安裝請在自己的 checkout 更新程式。

## 從私人 repo 遷移

原本私人 repo 已改名為 [orchd-archive](https://github.com/NatChung/orchd-archive)。公開版使用乾淨的新歷史，請保留舊 clone，另開新目錄：

```sh
# 在舊 clone 內，把 remote 指向 archive。
git remote set-url origin https://github.com/NatChung/orchd-archive.git
# 離開舊 clone，在尚不存在的新目錄 clone 公開版。
git clone https://github.com/NatChung/orchd.git orchd-public
uv tool install --force --refresh git+https://github.com/NatChung/orchd.git
uv tool list
orchd --help
orchd upgrade
```

重新安裝才會更換 uv 的更新來源；單改 clone remote 不會改變安裝來源。不要把公開版新歷史 pull 進舊私人 clone，也不要依賴 GitHub 舊名稱轉址。公開版用 HTTPS，不需要 GitHub token。新版 upgrade 會偵測 archive 或舊 SSH 安裝來源並提示重新安裝指令；舊版需先手動重新安裝，才會取得這個提示。

用 upgrade 顯示的 commit 核對公開版 main；只看套件版本不夠，版本號可能不變。資料庫與個人設定保留。等進行中的任務結束後，再以新程式開始 session 或 MCP process；upgrade 不會重啟既有 process。

## 問題與貢獻

一般 bug 與改善放在[公開 issues](https://github.com/NatChung/orchd/issues)，使用假資料並遮蔽 log。含客戶資訊、個資、內部網址或敏感設定的內容，放在有存取控制的私人 tracker；既有私人使用者可使用 orchd-archive。任何 issue 都不要貼 secrets。詳見 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 設定

預設位置與可覆寫的環境變數：

| 用途 | 預設 | 選項 |
| --- | --- | --- |
| 資料庫與任務狀態 | `~/.local/share/orchd` | `ORCHD_HOME` |
| 個人設定 | `~/.config/orchd` | `ORCHD_CONFIG_DIR` |
| Orch 根目錄 | `~/orch` | `ORCHD_ROOT` |
| Orch home | `~/orch/home` | `ORCHD_ORCH_HOME` |
| Desktop 入口 | `~/orch/interface` | `ORCHD_INTERFACE_HOME` |
| 專案目錄 | `~/projects` | `ORCHD_PROJECTS` |

`init.toml` 可設定 Orch home 額外讀取路徑與要停用的 apps；請依自己的工作範圍設定，範例不含實際 connector ID：

```toml
[home]
read = ["~/AGENTS.md", "~/agent-guidance"]
disabled_apps = []
```

選用 `gh-accounts.json` 可依 repo owner 路由 worker 的 GitHub CLI 帳號，不切換全域 active account：

```json
{"ExampleOwner": "example-personal", "*": "example-work"}
```

也可用 `ORCHD_GH_ACCOUNTS` 指定檔案。沒有 mapping 時沿用 gh 原本行為；既有 `GH_TOKEN` 保留。請把實際帳號、SSH keys、connector 與憑證設定放在本機。

`orchd doctor --profile example` 和 `python3 scripts/setup-wizard.py --profile example` 為選用的合成範例，不代表機器必須有四個 GitHub 帳號。一般使用者跑 `orchd doctor` 即可。自訂 profile 可用：

- doctor：`ORCHD_PROFILE_GH_ACCOUNTS`、`ORCHD_PROFILE_SSH_ALIASES`（逗號分隔）、`ORCHD_PROFILE_CREDENTIAL_DIR`。
- setup wizard：`ORCHD_PROFILE_ACCOUNTS_JSON`，格式 `{"example-personal": ["github-personal", "id_ed25519_personal"]}`；host alias 可為 null。
- 兩者的 connector 根目錄：`ORCHD_PROFILE_CONNECTOR_ROOT`。

doctor 僅檢查 credential 檔案存在性；wizard 列出手動步驟，不執行安裝、登入或設定寫入。

## 基本用法

```sh
orchd init
orchd binding
orchd list
```

在 `~/orch/home` 手動接受 Claude trust，再於 Codex Desktop 開啟 `~/orch/interface`、選擇 init 建立的 interface 權限，先要求 status，再交辦工作。

日常記住 `orch-restart`（重開 Orch，包含 adopt 與原有 Desktop 重新 bind）、`list`（任務）、`doctor`（檢查）與 `upgrade`（更新）即可。完整指令、全部旗標、worker 回報與重開／換電腦／升級流程，見[分組指令參考](docs/commands.zh-TW.md)。Worker 回報 done 仍待核對，部署與驗收另行確認。

另見 [CONTEXT.md](CONTEXT.md)、[架構決策](docs/adr/)、[入口](docs/entry.md)、[權限](docs/orch-permissions.md)、[目標與看板](docs/goals-board.md)與 [review／merge](docs/review-merge-policy.md)。

## 開發與檢查

```sh
uv venv
uv pip install -e .
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q orchd scripts tests
uv build
```

自動測試主要使用 mock 與暫存環境。人工 app-server acceptance 可執行 `python3 scripts/app_server_e2e.py`，需要 tmux、已登入的 Codex 與暫存環境；這不代表所有 GUI、provider 或版本組合都已驗證。

修改請開 branch 與 pull request，由另一位 reviewer 核對。提交問題或 log 前先遮蔽秘密與私人資料。

## 授權

[MIT](LICENSE)。
