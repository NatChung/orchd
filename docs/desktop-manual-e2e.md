# macOS Desktop 手動 E2E

在隔離環境驗證兩個真實 Codex Desktop 聊天。每個聊天各自啟動 Orch，切開再重開時接回原 Orch 及待答題，
並將回答原文送到原 worker。需要已登入的 Codex Desktop 和 Codex CLI、Claude CLI、Git 及 uv。
一般 PR CI 仍會跑 `tests/test_desktop_per_thread.py`。這個手動流程需要從 Desktop 輸入框送出交辦和回答，
才能在該聊天的本機紀錄看到使用者原文。

## 建立隔離環境

從要驗收的 orchd checkout 執行。保留同一個終端機視窗供後續步驟使用。

```sh
checkout="$(pwd -P)"
codex_bin="$(command -v codex)"
claude_bin="$(command -v claude)"
root="$(mktemp -d /private/tmp/orchd-desktop-e2e.XXXXXX)"
mkdir -p "$root"/{claude,init-codex,projects}
uv venv --python 3.13 "$root/venv"
uv pip install --python "$root/venv/bin/python" "$checkout"

git init -b main "$root/projects/probe"
git -C "$root/projects/probe" -c user.name='Orch E2E' -c user.email='orch-e2e@example.invalid' \
  commit --allow-empty -m 'Initialize local E2E fixture'
git clone --bare "$root/projects/probe" "$root/probe.git"
git -C "$root/projects/probe" remote add origin "$root/probe.git"

printf '%s\n' "$codex_bin" "$claude_bin" > "$root/provider-bins"
launcher="$root/venv/bin/orchd-e2e"
cat > "$launcher" <<PY
#!$root/venv/bin/python
import os
from pathlib import Path
from orchd.cli import main

root = Path(__file__).resolve().parents[2]
codex_bin, claude_bin = (root / "provider-bins").read_text(encoding="utf-8").splitlines()
os.environ.update(ORCHD_CODEX=codex_bin, ORCHD_CLAUDE=claude_bin)
for key, path in {
    "ORCHD_ROOT": root,
    "ORCHD_HOME": root / "state",
    "ORCHD_CONFIG_DIR": root / "config",
    "ORCHD_CONFIG_FILE": root / "config" / "init.toml",
    "ORCHD_ORCH_HOME": root / "home",
    "ORCHD_INTERFACE_HOME": root / "desktop-e2e-interface",
    "ORCHD_CHANNEL_HOME": root / "channel",
    "ORCHD_PROJECTS": root / "projects",
    "ORCHD_EXECUTABLE": Path(__file__).resolve(),
    "CLAUDE_CONFIG_DIR": root / "claude",
}.items():
    os.environ[key] = str(path)
raise SystemExit(main())
PY
chmod 700 "$launcher"
CODEX_HOME="$root/init-codex" "$launcher" init --entry-per-thread --no-trust
printf 'Desktop Local project folder: %s\n' "$root/desktop-e2e-interface"
```

啟動器也會為子程序指定測試路徑。Codex Desktop 和 CLI 沿用已登入的聊天紀錄，以讀取原生輸入和送回追問。
測試專案的 Git remote 指向本機臨時目錄。

登入隔離的 Claude 設定，然後在測試 Orch 家啟動一次 Claude，信任該資料夾後離開。

```sh
CLAUDE_CONFIG_DIR="$root/claude" "$claude_bin" auth login
(cd "$root/home" && CLAUDE_CONFIG_DIR="$root/claude" "$claude_bin")
```

在 Codex Desktop 用 **Local** 加入並信任上面印出的 Interface 資料夾，選擇 `interface` 權限。
在該專案內開兩個新聊天 A、B，分別請它呼叫 `orchd_entry.status`。記下兩個不同的 Orch ID，
並確認首次呼叫的 `started_new_orch` 為 `true`。

## 交辦、重開、回答

分別從 Desktop 輸入框貼到對應聊天並送出：

- **A**：請 relay 這則原文。DESKTOP_E2E_A_DISPATCH：在本機 repo probe 派一個 terra worker，使用 app-server、investigation。讓 worker 用 orchd ask 問「DESKTOP_E2E_A_PICK A or B?」，加上工具要求的摘要，然後等回答。Orch 請用 ask_nat 綁定 task 並用 quote_worker_question 送回這個聊天。收到答覆後用 answer 的 entry_reply_id 原樣轉給同一 worker。請 worker 回報收到的原文，Orch 核對後用 close(outcome=done) 結案。只准本機讀取及測試操作，不改檔、提交、推送或對外發送。
- **B**：請 relay 這則原文。DESKTOP_E2E_B_DISPATCH：在本機 repo probe 派一個 terra worker，使用 app-server、investigation。讓 worker 用 orchd ask 問「DESKTOP_E2E_B_PICK A or B?」，加上工具要求的摘要，然後等回答。Orch 請用 ask_nat 綁定 task 並用 quote_worker_question 送回這個聊天。收到答覆後用 answer 的 entry_reply_id 原樣轉給同一 worker。請 worker 回報收到的原文，Orch 核對後用 close(outcome=done) 結案。只准本機讀取及測試操作，不改檔、提交、推送或對外發送。

兩題在各自聊天出現後，先保留未答並存下當時的綁定和題號：

```sh
check="$checkout/scripts/desktop_manual_e2e.py"
"$root/venv/bin/python" "$check" pending "$root" > "$root/before.json"
cat "$root/before.json"
```

切離 A 和 B，再各從側欄重開原聊天。分別要求 `orchd_entry.status`，確認回報原 Orch 與原題號，
而且 `started_new_orch` 為 `false`。用收據比對兩次的綁定：

```sh
"$root/venv/bin/python" "$check" reopened "$root" --before "$root/before.json"
```

用實際題號替換下列 `N`，分別貼回原聊天：

- **A**：`回答 #N：選 A。DESKTOP_E2E_A_SELECTED A`
- **B**：`回答 #N：選 B。DESKTOP_E2E_B_SELECTED B`

等兩個 worker 回報並結案後，驗證 Desktop 原文、題號與 worker 的轉送收據：

```sh
"$root/venv/bin/python" "$check" answered "$root" --before "$root/before.json"
```

驗收成功時兩組均會印出聊天、Orch、題號、任務、回覆與 worker 收據 ID。
檢查器會確認兩組引用各自的 worker 提問、轉送逐字相同、任務結案且 worktree 已移除。
保留證據後，可用 `"$launcher" orch stop <測試 Orch ID>` 分別停止兩個測試 Orch，再從 Desktop 移除臨時專案。
