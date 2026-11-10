# Repository Hygiene

## Purpose

這是 repository 結構與整理的 canonical SOP：每一種檔案該放哪裡、怎麼命名、什麼時候歸檔或刪除，以及定期 audit 怎麼跑。`AGENTS.md` 只保存不可違反原則；worktree 的建立／整合／退役細節仍由 `runbooks/git-workflow.md` 擁有，本檔只定義「什麼時候該退役」與 audit 節奏。

2026-11-10 的一次性整理（搬移對照、執行紀錄）在 `documents/reports/2026-11-10-repo-reorganization.md`；本檔是之後每天都適用的規則。

## Layout contract

### Tracked top level

頂層只允許下列項目。新增頂層項目等於修改本表與 `tests/test_repo_layout.py` 的 allowlist，必須是刻意決定。

| 項目 | 內容 |
|---|---|
| `README.md` `AGENTS.md` `CLAUDE.md` `CONTRIBUTING.md` | 入口與 agent context |
| `PRODUCT.md` `MODEL.md` `DESIGN.md` `OPERATIONS.md` | 四份 owner 文件 |
| `pyproject.toml` `uv.lock` `.gitignore` | 專案設定 |
| `src/` | 共用程式（`aram_nn` package） |
| `scripts/` | CLI 入口與 launcher |
| `tests/` | 測試 |
| `runbooks/` | Agent-neutral SOP 與其附屬定義檔 |
| `documents/` | 其餘所有手寫文件 |
| `docs/` | 網站生成產物，不手改 |
| `.claude/` `.codex/` `.github/` | Adapter 與 CI |

### Handwritten documents

| 位置 | 放什麼 | 命名 |
|---|---|---|
| Root owner 文件 | 現行、長期有效的 product／model／design／operations 規則 | 固定檔名 |
| `runbooks/` | 需要照步驟執行的高風險操作 | `kebab-case.md` |
| `runbooks/task-definitions/` | Runbook 會引用的排程定義檔 | 與排程同名 |
| `documents/architecture/` | 長期有效的架構說明 | `kebab-case.md` |
| `documents/guides/` | 不是 SOP 的工作指南（實驗、資料使用） | `kebab-case.md` |
| `documents/reports/` | 有日期的報告、handoff、audit、一次性 spec | `YYYY-MM-DD-kebab-slug.md` |
| `documents/archive/` | 已被取代、只留作歷史的文件 | 保留原名；撞名時加 `-YYYY-MM-DD` |

- `documents/README.md` 是 `documents/` 唯一的索引；`runbooks/README.md` 是 `runbooks/` 唯一的索引。新增、搬移或歸檔文件時在同一個 commit 更新索引。子目錄不再各自放 README，因為多份索引會各自漂移。
- `documents/` 根目錄只放 `README.md`。
- 報告的日期是內容快照日，不是搬移日。
- 同一主題出新版時：新版進 `documents/reports/`，舊版移到 `documents/archive/` 並在檔頭加一行 `> Superseded by <相對路徑>`。不要讓兩份「看起來都是現行」的文件並存。
- 內容與現行 owner 文件重複的筆記不另存；把仍然有效的句子併回 owner 文件，其餘歸檔。

### Code

- 可重用邏輯進 `src/aram_nn/<plane>/`，入口用 `python -m aram_nn.<plane>.<module>`。
- `scripts/` 根目錄是凍結的：既有檔案不搬不拆（import 耦合與 live automation 都靠目前路徑），也不再新增檔案。清單固定在 layout test 的 baseline。
- 新的 script 放子目錄，且只能 import `aram_nn`，不能 import `scripts/` 根目錄的 flat module：

| 子目錄 | 用途 |
|---|---|
| `scripts/research/` | 一次性分析、ablation、實驗入口 |
| `scripts/ops/` | 營運工具、健康檢查 |
| `scripts/site/` | 網站 build 輔助 |
| `scripts/launchers/` | `.bat`／`.vbs`／`.ps1` 啟動器 |
| `scripts/templates/` `scripts/site_data/` | 既有的網站模板與資料 |

- 子目錄內的 script 用 `Path(__file__).resolve().parents[2]` 取 repo root。
- 暫時性 scratch 用 `scripts/_*.py`（gitignored），用完刪除；要留下來就改寫成正式入口。

### Local (ignored) files in the primary checkout

Primary checkout 頂層允許的 ignored 目錄：

| 目錄 | 內容 | 可否清理 |
|---|---|---|
| `data/` | Live DB、parquet、site snapshot | 不可。Lineage 與 live 資料只由 owner 決定 |
| `models/` | Live recommender model | 不可 |
| `logs/` | Live 與近期 log | 30 天以上且沒有 process 開著的可以隔離 |
| `outputs/` | 研究輸出、工具輸出、暫存媒體 | 見下 |
| `manual/` | 手寫 override CSV（程式會讀） | 不可 |
| `overwolf/` | Native app source，另外發佈 | 不可 |
| `.venv/` `dist/` `build/` `.pytest_cache/` `.ruff_cache/` | 環境、建置產物與工具快取 | 可重建 |
| `.claude/` `.codex/` `.impeccable/` | 工具狀態 | 工具自己管理 |

- `outputs/` 根目錄不放檔案。每個輸出進 `outputs/<category>/`；工具產生的 HTML（editor、annotator）進 `outputs/exports/`；一次性 probe 產物進 `outputs/_archive/<YYYY-MM-DD>-<topic>/` 或直接刪除。
- 報告附件（json／csv／tex／html）放 `documents/reports/_attachments/`，維持 ignored。
- Slide workspace、匯出的 pptx 等大型本機工作區放 sibling workspace（`aram-winrate-nn-workspace\`），不放進 repository。
- 不屬於上表的頂層 ignored 項目視為孤兒：歸位、隔離或刪除。

### Sibling workspace

`D:\Projects\CODING\aram-winrate-nn-workspace\` 放所有不屬於 primary checkout Git scope 的東西：

| 目錄 | 內容 | 保留規則 |
|---|---|---|
| `worktrees\<task>` | Task worktree | 見 Worktree lifecycle |
| `backups\<topic>-<YYYYMMDD>` | Bundle、cutover 快照、退役時備份的 untracked 檔 | 至少 60 天；之後由 audit 列為候選，owner 決定刪除 |
| `trash\<YYYY-MM-DD>\` | 隔離區（見 Deletion policy） | 至少 14 天；永久刪除需 owner 確認 |
| 其他工具目錄 | 與 repo 無 Git 關係的工具或素材 | 各自決定 |

`D:\Projects\CODING\arammeta` 是指向 primary checkout 的 junction。它不是副本，任何遞迴刪除都會刪到 primary checkout 本體。

## Worktree lifecycle

建立、更新、整合的指令在 `runbooks/git-workflow.md`。這裡只定義退役條件：

| 狀態 | 判定 | 動作 |
|---|---|---|
| Active | 7 天內有 commit、檔案變動，或有 process 在用 | 不動 |
| Clean + merged + idle ≥ 7 天 | `status --short` 無輸出，且 `merge-base --is-ancestor <HEAD> origin/main` 成立 | 退役 |
| Merged，但有 untracked 檔 | 同上，但 `status --short` 只有 `??` | Untracked 檔先搬到 `backups\`，再退役 |
| Clean，內容已在 main 但 commit 不是 ancestor | `git merge-tree --write-tree origin/main <HEAD>` 的 tree 等於 `origin/main^{tree}` | Bundle 已含該 commit 才退役；branch 保留 |
| Unmerged 或有 tracked 修改 | 其餘 | 保留並記錄原因；由 owner 決定 |

- 退役前掃描 worktree 內的 reparse point（junction／symlink）。指向 worktree 外部的連結存在時不退役，回報給 owner；`git worktree remove` 與任何遞迴刪除都可能刪穿連結。
- 退役只用 `git worktree remove <path>`（不加 `--force`）與 `git worktree prune`。
- Local branch 只有在是 `origin/main` 的 ancestor 且沒有 worktree 時才用 `git branch -d` 刪除。Unmerged branch 與 remote branch 只列為候選。
- 新的 task worktree 一律建在 `aram-winrate-nn-workspace\worktrees\`。位置不合規的既有 worktree 在 idle 時用 `git worktree move` 搬入。

## Deletion policy

| 類別 | 例子 | 處理 |
|---|---|---|
| 空目錄 | 殘留的 temp 目錄 | 確認為空且不是 reparse point 後，用 `[System.IO.Directory]::Delete($path, $false)` 移除 |
| 可重建 | `node_modules`、`__pycache__`、`graphify-out`、preview render | 隔離；14 天後可刪 |
| 不可重建但已過時 | 舊 log、舊 QA 截圖、舊媒體輸出 | 隔離並列出內容；永久刪除需 owner 確認 |
| Lineage／live | `data/`、`models/`、baseline 輸出 | 不動；只在 audit 報告列出大小與日期 |

隔離＝同磁碟搬移到 `aram-winrate-nn-workspace\trash\<YYYY-MM-DD>\<原始相對路徑>`，並在同一層寫 `MANIFEST.md`（原路徑、大小、最新 mtime、還原指令）。隔離是可逆的；永久刪除不是，所以分成兩步。

絕不對下列路徑做搬移或刪除：`data\lcu\games.db*`、`models\`、被 process 開著的檔案、任何 tracked 或未 commit 的 WIP 檔案。

## Audit

```powershell
python -m aram_nn.repo_audit
```

Audit 只讀不寫。輸出分三級：

| 等級 | 內容 | 意義 |
|---|---|---|
| FAIL | Tracked 頂層不在 allowlist、`scripts/` 根目錄出現新檔、手寫 Markdown 位置不合規、報告檔名不合規、文件沒有列在索引 | 與 `tests/test_repo_layout.py` 相同的檢查；必須修到 0 |
| WARN | 頂層孤兒 ignored 項目、`outputs/` 根目錄的散檔、可退役的 worktree、位置不合規的 worktree、已 merge 的 local branch、超過 60 天的 backup、超過 14 天的 trash | 待整理清單 |
| INFO | 主要本機目錄大小、D 槽剩餘空間 | 觀察用 |

節奏：

- Task 收尾：新增、搬移過文件或 script 的 task 在 commit 前跑 `pytest tests/test_repo_layout.py`，FAIL=0 才算完成。
- Task merge 後：照 `runbooks/git-workflow.md` 立即做 retire preflight。
- 每次換 patch（約兩週）或 D 槽剩餘空間低於 50 GB：跑一次完整 audit，處理 WARN。
- 處理 WARN 時依 Deletion policy 與 Worktree lifecycle；不確定就隔離或保留，並回報。

## Common placement questions

| 我有一個… | 放哪裡 |
|---|---|
| 新的分析報告 | `documents/reports/YYYY-MM-DD-slug.md`，並加進 `documents/README.md` |
| 給下一個 agent 的 handoff | 同上；被新 handoff 取代後移到 `documents/archive/` |
| 新的操作流程 | `runbooks/<name>.md`，並加進 `runbooks/README.md` |
| 現行規則的修改 | 直接改對應的 owner 文件，不另開筆記 |
| 一次性分析 script | `scripts/research/<name>.py`（只 import `aram_nn`） |
| 會被多處使用的邏輯 | `src/aram_nn/<plane>/` |
| 實驗輸出、圖、暫存 parquet | `outputs/<category>/` |
| 手動調校用的 HTML 工具輸出 | `outputs/exports/` |
| Bundle、切換前快照 | `aram-winrate-nn-workspace\backups\<topic>-<YYYYMMDD>\` |
