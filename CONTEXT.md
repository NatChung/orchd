# orchd

由 Orch 派工、worker 執行，讓多件工作並行且可管理。

## Language

**Orch**：
在 Orch 家開啟的 session，只負責拆工作、派工、核對回報；自己不讀寫任何專案 repo。
_Avoid_：協調者、主 session

**管理組（Group）**：
一個 Orch 與它派出的 workers。每開一個 Orch session 就是一組，可同時存在多組。
_Avoid_：團隊、workspace

**入口（Entry）**：
在 Codex Desktop 讓 Nat 交辦與回答追問的 session；只把原文轉給綁定的那一個 Claude Orch、再把 Orch 的原文帶回來，
不是 Orch、不派工、不開組。資料夾與指令叫 desk（`~/projects/desk`、`orchd desk`）。見 ADR-0003。
_Avoid_：前台 Orch、代理人

**Worker**：
為單一任務啟動、在該任務的 worktree 內工作並回報的 Agent；任務結束即停止。
_Avoid_：長駐 worker、專案 worker

**任務（Task）**：
Orch 派給一個 worker 的一份工作，派出時即寫明完成條件。
_Avoid_：job、ticket（ticket 指 repo 自己的 issue）

**交辦**：
你交給 Orch 的一件事；跨 repo 或需拆成多個任務時，以 Orch issue 記錄。
_Avoid_：總任務

**回報（Report）**：
worker 交回的結果與證據（commit、測試結果、未完成事項）。回報本身不代表完成。

**完成**：
Orch 對照任務的完成條件核對回報後所做的判定。

**Orch 家**：
`~/projects/orch`，Orch 唯一能讀寫的目錄，存放 Orch 的指示與筆記。
_Avoid_：把工具程式碼放進 Orch 家

**未結任務**：
尚未判定完成或尚未由你關閉的任務；任何時候都能列出。

**對外確認**：
worker 對外寄送前，把預覽原文經 Orch 轉給你、取得你的明確同意。
