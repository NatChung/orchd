# Orch：先有票，再派工

Operator 的新交辦，不論問答或實作，都先找現成票；沒有就先建立 GitHub
issue，或取得既有 Jira ticket 的參照，之後才派執行 worker；版本支援時 dispatch 帶 `issue_ref`，
舊版則在 instructions 寫明主票並回報依賴未就緒（見第 3 節）。
同一張票可以拆成多個 worker 任務，每個任務只有一個主參照。

流程原則：外部 tracker 是工作目的、範圍、驗收與票狀態的唯一來源；
orchd DB 保存 worker 問答、訊號與執行證據，兩者只用主票參照連結。
既有資料原地保留，不搬移、不互抄。此文件定義流程，不宣告已安裝版本的功能。

## 1. 找到主票

1. 確認專案與指定 tracker，依該 repo 的 `docs/agents/issue-tracker.md`
   和 `docs/agents/triage-labels.md`，核對實際 repo、帳號、角色 mapping。
   設定缺失或互相矛盾時先請 Operator 確認，不從 origin 猜 tracker。
   Orch 仍依既有權限工作，不因此取得專案 repo 的直接讀寫權限。
2. 在指定 tracker 查現成票，核對範圍、驗收條件與本次交辦是否相符；
   符合就沿用。背景 PR、教材案例或 goals.source 的來源票不能自動當主票。
3. 無符合的 GitHub issue 時，準備標題、工作目的、範圍、驗收條件、
   背景連結與預定 labels，由開票 worker 按下節權限開票。Jira 路由沒有現成 ticket 時，
   請 Operator 或其指定的 Jira 維護者提供或建立票並給參照；
   Orch 不直接建立、留言或修改 Jira，也不以此流程派 Jira 寫入 worker。
4. GitHub 開票後 read-back，確認它是 issue 而非 PR、內容正確且可存取，
   使用 API 回傳的 canonical owner/repo 與正整數票號。
   Jira 記 `jira:KEY-N`（key 大寫）；無法唯讀核對時明示未核對，
   不把格式驗證當成票存在、權限或狀態證據。

Orch 不跑 shell，repo 查詢與開票交給前置 worker；開票任務要明示
目的地與允許動作。前置 worker 尚無主票時在 instructions 註明「待建立主票」，
不假造 issue_ref；開票後回報 read-back 與參照，支援 task_link 的版本再補
前置任務的正式關聯。這是建立主票的前置步驟，主票確認後才派問答／實作 worker。

完成此步的證據是主票連結與正式參照；草稿或「準備開票」不算已有票。

## 2. 開票權限與 labels

| 動作／目的地 | Orch 流程 |
| --- | --- |
| 在 `NatChung/*` 建 GitHub issue | Orch 明確派出的開票 worker 可免預覽；開後 read-back |
| 在客戶／公司 repo 建 issue | 將完整預覽給 Operator，取得核准後才開；開後 read-back |
| Jira | 只記既有參照，不直接寫 Jira |
| 建立缺少的 GitHub label | 獨立維護任務，精確預覽並核准；不隨開票自動建立 |

公司與客戶 repo 的預覽必須含目的地 repo、標題、完整 body、將套用的
現存 labels，以及缺少的 labels。未知 owner 的權限先問 Operator。
免預覽適用於 Orch 明確派去為 Operator 建立 NatChung issue 的 worker，
只限該次 issue 建立；其他 worker 寫入仍走 `orchd ask`，不延伸到 PR、
留言、修改、關票或 label 維護。派工必須明示此例外的 repo 與開票範圍；
repo guide 的預覽規則見 [issue tracker](agents/issue-tracker.md)。

以 tracker guide 的 mapping 作期望、live labels 作實際，逐個角色比對。
只套已存在且 mapping 正確的 label，不自動建、改名或刪除 label。
缺 label 時可省略缺項，預覽註明「缺少 X，本次不套用」；若 repo 規則
要求該 label，先完成獨立維護或取得 Operator 的處理決定，再開票。
NatChung 免預覽開票也要在開票回報註明缺項；需要預覽時一併列出。
缺 `needs-triage` 時，開票命令省略 `--label needs-triage`，套用其他已存在
且適用的 labels，並在預覽／免預覽回報列明缺項與實際清單；
若 repo 明定 needs-triage 必填，先請 Operator 決定或完成獨立維護再開票。
Jira 不套 GitHub 五角色，legacy local phase 不當作 GitHub label。
可交辦的 dry-run 清單見 [label reconciliation](examples/tracker-label-reconciliation.md)。

開票 worker 的 GitHub CLI 每次明示 `--repo`，保留既有 `GH_TOKEN`；沒有時用 repo 對應
帳號的子程序憑證，不輸出 token、不改全機 active account（不使用
`gh auth switch`）。憑證與真實 route 設定留本機。

## 3. 帶參照 dispatch，從 DB 查執行

正式格式只有 `github:owner/repo#N` 與 `jira:KEY-N`。dispatch 內的
`issue_ref` 指主票，不是 issue URL、PR URL 或自由文字。

```text
Operator 交辦 → 前置 worker 查／建立主票 → 核對參照 → 派執行 worker
查 to-do → GitHub issue／Jira
查問答與執行 → orchd snapshot／inbox → issue_ref 相同的 tasks → DB 紀錄
```

派工只寫 worker 的執行目標、操作限制、允許動作與 `done_when` 驗證步驟，
不要貼整份 issue body。worker 唯讀查原票取得完整範圍。
拆工、review、返工都沿用主票 ref；背景票仍是背景連結。
scope 或驗收需要調整時，GitHub 由取得相應寫入授權的 worker 更新工作定義；
開票免預覽不授權後續改票。Jira 由 Operator 或其指定的 Jira 維護者在 Jira
更新，Orch 只記參照並等待確認，不直接寫入，也不派 worker 代寫 Jira。
worker ask、Operator 回答、訊號、progress、report 留 DB，不鏡像到票的留言或本文。

### 版本依賴與舊版退路

[PR #25](https://github.com/NatChung/orchd/pull/25) 的 `e34b5e3` 定義以下
contract；合併並升級前，使用中的 CLI／MCP 不一定提供這些功能。
核對 MCP dispatch schema 與 CLI `--help`，升級後重啟必要的 session／MCP。

- MCP：dispatch 可選 `issue_ref`；`task_link` 使用 `task_id`、`ref`，
  `task_unlink` 使用 `task_id`。
- CLI：`orchd task link --task ID --ref REF [--actor orch|operator] [--idempotency-key K]`；
  `orchd task unlink --task ID`。
- snapshot v2：task 的 `issue_ref`，以及 `issue_links` 的
  `version`、`available`、`history`；用 `orchd snapshot --json --schema-version 2`
  讀取。缺 metadata 的舊版不代表所有 task 都已確認無主票。

```sh
orchd task link --task example-task --ref github:example-owner/example-app#12 --actor orch --idempotency-key example-link-1
orchd task unlink --task example-task
```

如果 dispatch 沒有 `issue_ref` 參數，在 `instructions` 寫明正式主票參照與
連結，回報「issue_ref 依賴未就緒」，沿既有 dispatch 執行；
不要傳不支援的參數，也不要宣稱文字已成為正式關聯。升級後逐筆核對，
透過 task_link／CLI link 補關聯，不自動回填歷史任務。
既有關聯修改前核對目前參照；此介面不提供舊值條件更新保護。遇到歸屬爭議先確認，不直接覆寫。

查 snapshot v2 時選同 ref 的 tasks，再用 task id 找 DB 問答／progress／report；
舊版退路期間只以已核對的 task id 找 DB 紀錄，不從自由文字自動推定關聯。
沒有 task 的 open 票就是尚未派工。
同 ref 的票已 closed 而 task 未結時，保留兩套狀態並指出差異。
report done 不自動關票、close task 不代表票已驗收；票 closed 也不自動 close task。

## 4. local-markdown 過渡與 routes 範例

local-markdown 專案的既有 Markdown 原地保留，不搬、不刪、
不批量補開票。新工作沿用或建立 GitHub issue，已有 Jira 產品票則留 Jira。
舊 Markdown 需要再派工時，只補外部票連結；外部票寫本次範圍／驗收，
附舊文件背景連結，不複製歷史全文，不鏡像外部票狀態，也不新增平行 Markdown to-do。
舊工具仍依賴 phase 的工作維持兼容流程，限制寫入 worker 指令；
此流程不重構工具、不做雙向狀態同步。

各 repo 的 tracker guide 修正與 label 維護另派 repo owner；此文件只提供流程。
[合成 routes](examples/project-routes.json) 示範專案到外部 tracker 的路由結構，所有 repo、
帳號與 Jira site 都是虛構，不能直接作 production 設定；真設定只納入
已核對的 active project，憑證留 server，不回 browser。

人工走查合成例子：選 `example-app` 的 GitHub route，唯讀取得 issue #12，
核對 `github:example-owner/example-app#12`，dispatch 帶同 ref；再從 snapshot
選該 ref 的 tasks，依 task id 查 DB 執行紀錄。Jira 例子只記 `jira:DEMO-7`，
不寫 Jira；reader 不可用時明示 unavailable，不當成無票或已關票。

已安裝的 Orch home 指示由 Operator 維護；repo 的
`orchd/templates/home/AGENTS.md` 只提供範本，Operator 決定是否套用，
不執行 init 覆寫個人檔案。
