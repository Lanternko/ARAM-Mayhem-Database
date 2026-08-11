# Runbooks

這裡是 agent-neutral 的操作 SOP。`AGENTS.md`、Claude/Codex skills 與其他工具入口只能連到這裡，不得各自維護另一份命令或 artifact 清單。

## Canonical runbooks

- `git-workflow.md` — human／agent worktree lifecycle、fetch／pull／rebase／merge／push 與安全清理。
- `site-deploy.md` — GitHub Pages routine data、frontend shell、generator／schema 三條發布 lane，以及 deploy intent、排程與驗證。
- `crawler-stall.md` — Mayhem crawler 停滯診斷、LCU recovery 與 production watchdog。
- `opgg-seed-refresh.md` — OPGG page window refresh、seed hydration 與成功判準。

## Ownership rule

- 高階目的與不可違反原則：`AGENTS.md`。
- Live process 拓撲、owner 與 production profile：`OPERATIONS.md`。
- 一般 Git／worktree lifecycle：`git-workflow.md`；automation 只能在自己的 runbook 定義例外。
- 精確操作與 rollback：本目錄。
- CLI 完整參數：該命令的 `--help`。
- `.claude/skills/` 與 `.codex/skills/` 只是自動發現用 adapter；若與本目錄衝突，以本目錄為準。

修改 runbook 時要同時檢查 `AGENTS.md` 與 `OPERATIONS.md` 的指向，但不要把完整步驟複製回上層文件。
