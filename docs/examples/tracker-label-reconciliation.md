# Label reconciliation：dry-run 交辦稿

本清單為合成例子，沒有查 live labels、沒有建立或更改任何 label。
實際開票權限見 [workflow](../issue-linked-workflow.md#2-開票權限與-labels)。

## Dry-run

在指定 repo 讀 `docs/agents/issue-tracker.md`、`triage-labels.md`，再唯讀
取得完整分頁的 live labels。每個角色填 expected、actual 與 missing，
mapping 不存在時標「待確認」，不擅自套五角色。保留已存在 label 的名稱、
顏色、描述及 issue 分類。

| 角色 | guide 指定名稱 | live 是否存在 | dry-run 結果 |
| --- | --- | --- | --- |
| needs-triage | needs-triage | 是 | 保留 |
| needs-info | needs-info | 否 | 缺 needs-info |
| ready-for-agent | ready-for-agent | 否 | 缺 ready-for-agent |
| ready-for-human | ready-for-human | 否 | 缺 ready-for-human |
| wontfix | wontfix | 是 | 保留 |

## 獨立維護預覽

另派 label 維護任務。將下列完整預覽交 Operator，取得精確核准後才寫入：

```text
目的地：example-company/example-hub（合成）
只建立缺少的三個 label；不修改已有 label，不重分類 issues。
name: needs-info; color: d4c5f9; description: Waiting on reporter for more information
name: ready-for-agent; color: 0e8a16; description: Fully specified, ready for an AFK agent
name: ready-for-human; color: 1d76db; description: Requires human implementation
```

執行前重新讀 live labels，已補上的項目略過；新增項目的
name/color/description 必須符合核准內容。每項寫入後 read-back 核對三欄，
回報實際新增、原已存在、失敗與未驗證項目。不借此改現存 label 或大量補票。
文件 setup 完整與 live labels 齊全分別驗收。

若此次開票先省略缺項，issue 預覽／免預覽開票回報列出缺 label 與實際套用
清單；repo 要求 label 必填時先處理決定，再送出開票命令。
Jira 不做此 label 維護；legacy local phase 保持既有 mapping，
新 GitHub 工作才依 GitHub guide 比對。

缺 `needs-triage` 時，省略開票命令的 `--label needs-triage`，在預覽或
免預覽回報中列明缺項；若 repo 明定必填則先請 Operator 決定，不送失敗的
開票命令，也不自動建立 label。
