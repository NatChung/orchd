# Orch：先有票，再派工

Nat 的新交辦，不論問答或實作，都先找現成票；沒有就先建立 GitHub
issue，或取得既有 Jira ticket 的參照，之後才 dispatch，並帶 `issue_ref`。
同一張票可以拆成多個 worker 任務，每個任務只有一個主參照。

依據：[三欄看板設計](https://github.com/NatChung/orch-dashboard/blob/24f7d57/docs/design/three-pane-issues.md)
第 6 節與工作包 D；開票權限以本次 Nat／Orch 決定為準。本文件是流程規則，
不是 CLI 實作或已安裝版本的功能宣告。

## 1. 找到主票

1. 確認專案與指定 tracker，依該 repo 的 `docs/agents/issue-tracker.md`
   和 `docs/agents/triage-labels.md`，核對實際 repo、帳號、角色 mapping。
   設定缺失或互相矛盾時先請 Nat 確認，不從 origin 猜 tracker。
   Orch 仍依既有權限工作，不因此取得專案 repo 的直接讀寫權限。
2. 在指定 tracker 查現成票，核對範圍、驗收條件與本次交辦是否相符；
   符合就沿用。背景 PR、教材案例或 goals.source 的來源票不能自動當主票。
3. 無符合的 GitHub issue 時，準備標題、工作目的、範圍、驗收條件、
   背景連結與預定 labels，按下節權限開票。Jira 路由沒有現成 ticket 時，
   請 Nat 提供或建立票並給參照；Orch 不直接建立、留言或修改 Jira。
4. GitHub 開票後 read-back，確認它是 issue 而非 PR、內容正確且可存取，
   使用 API 回傳的 canonical owner/repo 與正整數票號。
   Jira 記 `jira:KEY-N`（key 大寫）；無法唯讀核對時明示未核對，
   不把格式驗證當成票存在、權限或狀態證據。

完成此步的證據是主票連結與正式參照；草稿或「準備開票」不算已有票。

## 2. 開票權限與 labels

| 動作／目的地 | Orch 流程 |
| --- | --- |
| 在 `NatChung/*` 建 GitHub issue | 可直接開，不需預覽；開後 read-back |
| 在客戶／公司 repo 建 issue | 將完整預覽給 Nat，取得核准後才開；開後 read-back |
| Jira | 只記既有參照，不直接寫 Jira |
| 建立缺少的 GitHub label | 獨立維護任務，精確預覽並核准；不隨開票自動建立 |

公司與客戶 repo 的預覽必須含目的地 repo、標題、完整 body、將套用的
現存 labels，以及缺少的 labels。未知 owner 的權限先問 Nat。
免預覽只適用 Orch 在 NatChung repo 建 issue，不授權 worker 略過其
`orchd ask` 規則，也不授權 PR、留言、修改、關票或其他對外動作。

以 tracker guide 的 mapping 作期望、live labels 作實際，逐個角色比對。
只套已存在且 mapping 正確的 label，不自動建、改名或刪除 label。
缺 label 時可省略缺項，預覽註明「缺少 X，本次不套用」；若 repo 規則
要求該 label，先完成獨立維護或取得 Nat 的處理決定，再開票。
NatChung 免預覽開票也要在開票回報註明缺項；需要預覽時一併列出。
Jira 不套 GitHub 五角色，legacy local phase 不當作 GitHub label。
可交辦的 dry-run 清單見 [label reconciliation](examples/tracker-label-reconciliation.md)。

GitHub CLI 每次明示 `--repo`，保留既有 `GH_TOKEN`；沒有時用 repo 對應
帳號的子程序憑證，不輸出 token、不改全機 active account（不使用
`gh auth switch`）。憑證與真實 route 設定留本機。

## 3. 帶參照 dispatch，從 DB 查執行

正式格式只有 `github:owner/repo#N` 與 `jira:KEY-N`。dispatch 內的
`issue_ref` 指主票，不是 issue URL、PR URL 或自由文字。

```text
Nat 交辦 → 查／建立主票 → read-back／記錄參照 → dispatch(issue_ref)
查 to-do → GitHub issue／Jira
查問答與執行 → orchd snapshot／inbox → issue_ref 相同的 tasks → DB 紀錄
```

派工只寫 worker 的執行目標、操作限制、允許動作與 `done_when` 驗證步驟，
不要貼整份 issue body。worker 唯讀查原票取得完整範圍。
拆工、review、返工都沿用主票 ref；背景票仍是背景連結。
scope 或驗收需要調整時，按既有寫入授權更新外部票的工作定義；
worker ask、Nat 回答、訊號、progress、report 留 DB，不鏡像到票的留言或本文。

工作包 A 提供 MCP dispatch 的 `issue_ref`、task link/unlink CLI 與
snapshot 參照欄位。部署前核對使用中的 MCP schema／CLI `--help` 與
snapshot `issue_links.available`；升級後重啟必要的 session／MCP。
缺 capability 時回報依賴未就緒，保留票參照，等 A 可用再做新 dispatch；
不要把 instructions 裡的字串宣稱成已持久化的 `issue_ref`。
以下是 A 的提議命令，須以落地版本的 help 為準：

```sh
orchd task issue-link TASK --issue-ref github:example-owner/example-repo#12 --expected-issue-ref none --reason "Verified parent issue"
orchd task issue-unlink TASK --expected-issue-ref github:example-owner/example-repo#12 --reason "Incorrect parent confirmed"
```

既有 task 不自動回填；只有逐筆核對主票後才 link。CAS 比對舊參照，
衝突時重新核對，不覆蓋別人的關聯。查 snapshot 時選同 ref 的 tasks，
再用 task id 找 DB 問答／progress／report；沒有 task 的 open 票就是尚未派工。
同 ref 的票已 closed 而 task 未結時，保留兩套狀態並指出差異。
report done 不自動關票、close task 不代表票已驗收；票 closed 也不自動 close task。

## 4. local-markdown 過渡與 routes 範例

local-markdown 專案的既有 Markdown 原地保留，不搬、不刪、
不批量補開票。新工作沿用或建立 GitHub issue，已有 Jira 產品票則留 Jira。
舊 Markdown 需要再派工時，只補外部票連結；外部票寫本次範圍／驗收，
附舊文件背景連結，不複製歷史全文，不鏡像外部票狀態，也不新增平行 Markdown to-do。
舊工具仍依賴 phase 的工作維持兼容流程，限制寫入 worker 指令；
本工作包不重構工具、不做雙向狀態同步。

各 repo 的 tracker guide 修正與 label 維護另派 repo owner；本包只提供流程。
[合成 routes](examples/project-routes.json) 示範設計 C1 的結構，所有 repo、
帳號與 Jira site 都是虛構，不能直接作 production 設定；真設定只納入
已核對的 active project，憑證留 server，不回 browser。

人工走查合成例子：選 `example-app` 的 GitHub route，唯讀取得 issue #12，
核對 `github:example-owner/example-app#12`，dispatch 帶同 ref；再從 snapshot
選該 ref 的 tasks，依 task id 查 DB 執行紀錄。Jira 例子只記 `jira:DEMO-7`，
不寫 Jira；reader 不可用時明示 unavailable，不當成無票或已關票。

已安裝的 `~/orch/home/AGENTS.md` 由 Nat 維護；本包只修改
`orchd/templates/home/AGENTS.md`，由 Nat 決定套用 diff，不執行 init 覆寫。
