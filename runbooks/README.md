# Runbooks

這裡是 agent-neutral 的操作 SOP。`AGENTS.md`、Claude/Codex skills 與其他工具入口只能連到這裡，不得各自維護另一份命令或 artifact 清單。這是 `runbooks/` 唯一的索引；新增 runbook 時在同一個 commit 加進來（`tests/test_repo_layout.py` 會檢查）。

## Canonical runbooks

- [`git-workflow.md`](git-workflow.md) — human／agent worktree lifecycle、fetch／pull／rebase／merge／push 與安全清理。
- [`repo-hygiene.md`](repo-hygiene.md) — 檔案放置、文件命名與歸檔、worktree 退役條件、本機檔案隔離與 `repo_audit` 節奏。
- [`site-deploy.md`](site-deploy.md) — GitHub Pages routine data、frontend shell、generator／schema 三條發布 lane，以及 deploy intent、排程與驗證。
- [`crawler-stall.md`](crawler-stall.md) — Mayhem crawler 停滯診斷、LCU recovery 與 production watchdog。
- [`single-writer-cutover.md`](single-writer-cutover.md) — 兩個直連 SQLite snowball worker 切成單一 `snowball-workers` fleet（RPC producers + 一個 writer）。
- [`opgg-seed-refresh.md`](opgg-seed-refresh.md) — OPGG page window refresh、seed hydration 與成功判準。
- [`player-history.md`](player-history.md) — player-history snapshot builder 與公開服務的 security boundary、資料來源與驗證。
- [`desktop-release.md`](desktop-release.md) — Windows 桌面 recommender（`ARAMRecommender.exe`）的 release contract、manifest 與自動更新驗證。

## Supporting files

- `task-definitions/` — runbook 引用的 Windows 排程定義檔（目前是 `MayhemLCUWatchdogKeepalive.xml`，由 `OPERATIONS.md` 的重建流程使用）。

## Ownership rule

- 高階目的與不可違反原則：`AGENTS.md`。
- Live process 拓撲、owner 與 production profile：`OPERATIONS.md`。
- 一般 Git／worktree lifecycle：`git-workflow.md`；automation 只能在自己的 runbook 定義例外。
- 精確操作與 rollback：本目錄。
- CLI 完整參數：該命令的 `--help`。
- `.claude/skills/` 與 `.codex/skills/` 只是自動發現用 adapter；若與本目錄衝突，以本目錄為準。

修改 runbook 時要同時檢查 `AGENTS.md` 與 `OPERATIONS.md` 的指向，但不要把完整步驟複製回上層文件。
