<!-- lines: 35 -->
# Claude Code Adapter

## Why

`AGENTS.md` 是本 repository 唯一 canonical agent context。Claude Code 進入專案後先完整閱讀 `AGENTS.md`；本檔只保存 Claude-specific routing，不複製專案背景、命令、NEVER 或設計規則。

## Architecture

- Product、model、design、operations 的 owner 依序是 `PRODUCT.md`、`MODEL.md`、`DESIGN.md`、`OPERATIONS.md`。
- 精確高風險操作在 `runbooks/`；`.claude/skills/` 只負責把自然語言意圖導向相同 runbook。
- `docs/` 是網站生成產物；手寫 Markdown 放根目錄、`notes/`、`documents/` 或 `runbooks/`。

## Commands

不要在本檔新增 command。一般入口看 `scripts/README.md` 與目標 CLI `--help`；live process 看 `OPERATIONS.md`；發布、crawler recovery 與 seed refresh 看 `runbooks/README.md`。

## NEVER

- Never 讓本檔和 `AGENTS.md` 各自維護完整規則；兩份會 drift，且不同 agent 會得到不同專案。
- Never 把 skill 內的命令或 artifact list 當 canonical；skill 必須讀取對應 runbook。
- Never 以本檔覆蓋 `AGENTS.md`。若兩者衝突，以 `AGENTS.md` 為準並修正本 adapter。

## Scoped Rules

- 網站發布：`.claude/skills/deploy-tier-list/SKILL.md` 或 `.claude/skills/deploy-shell/SKILL.md` → `runbooks/site-deploy.md`。
- 尚未發布的 Articles/column feature：`.claude/skills/write-column/SKILL.md` 只提供 dormant guard，不可修改舊單檔 `ARTICLES` contract。
- 其他自動發現 skill 可以補充任務流程，但不能重新定義 root project policy。

## How to edit this file

- Keep this file under 60 lines.
- 只新增 Claude-specific routing；跨 agent 規則回寫 `AGENTS.md` 或 canonical 二層文件。
- 任何新 skill 只能指向一個明確 owner，不得複製整份 SOP。
- 修改後更新頂端 line count。
