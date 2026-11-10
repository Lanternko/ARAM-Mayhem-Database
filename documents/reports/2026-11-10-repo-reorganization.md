# Repository Reorganization Spec (2026-11-10)

這是一次性的整理 spec。之後持續適用的規則在 `runbooks/repo-hygiene.md`；本檔記錄這次要搬什麼、刪什麼、怎麼驗證，以及執行紀錄。

## 1. 目標與範圍

1. Tracked 頂層從 23 項降到 19 項，手寫文件只剩三個家：root owner 文件、`runbooks/`、`documents/`。
2. 合併重複文件：`notes/` 併入 `documents/`；四份索引 README 併成一份；`MODEL_NOTES` 兩個版本併成一份；被取代的報告歸檔；報告檔名統一。
3. 退役已 merge 且閒置的 worktree，清掉已 merge 的 local branch。
4. Primary checkout 的 ignored 孤兒歸位或隔離。
5. 把規則落成可執行的檢查：`tests/test_repo_layout.py` 與 `python -m aram_nn.repo_audit`。

不在這次範圍（原因見 §7）：`scripts/` 根目錄 122 個檔案的實體搬移、`data/` 與 `models/` 內的任何東西、slide workspace 搬遷、remote branch、任何永久刪除。

工作拆成兩個互不重疊的部分：

| Part | 內容 | 工作目錄 | 產出 |
|---|---|---|---|
| A | Tracked 結構、文件合併、檢查工具 | `D:\Projects\CODING\aram-winrate-nn-workspace\worktrees\repo-reorg`（branch `codex/repo-reorg`） | Branch 上的 commits；不 push、不 merge |
| B | Worktree／branch 退役、本機 ignored 檔整理 | `D:\Projects\CODING\aram-winrate-nn-workspace\backups\reorg-20261110` | 本機狀態改變＋`execution-log.md` |

## 2. 全域安全規則（A、B 都適用）

- Primary checkout `D:\Projects\CODING\aram-winrate-nn` 是 live harness anchor，而且有其他 session 未 commit 的 WIP。**不得**在 primary checkout 執行會改 index 或 working tree 的 Git 指令（`add`、`commit`、`checkout`、`switch`、`restore`、`reset`、`stash`、`pull`、`merge`、`rebase`、`clean`）。**不得**修改、搬移或刪除 primary checkout 內任何 tracked 檔案或 `git status --short` 列出的檔案。
- 絕不碰：`data\`（全部，特別是 `data\lcu\games.db*`）、`models\`、`overwolf\`、`manual\`、`.venv\`、`.git\`、`docs\`。
- 絕不碰這三個 checkout：primary、`D:\Projects\CODING\aram-winrate-nn-api-runtime`、路徑含 `arammeta-publish-` 的 publisher 暫存 worktree。
- `D:\Projects\CODING\arammeta` 是指向 primary checkout 的 junction，不得刪除或遞迴操作。
- 刪除目錄前先確認不是 reparse point。只有空目錄可以直接移除，用 `[System.IO.Directory]::Delete($path, $false)`。不使用 `Remove-Item -Recurse`、`rmdir /s`、`rm -rf`。
- 不 `git add .`／`-A`、不 `--force`、不 stash、不 push、不 merge 到 `main`、不刪 remote branch。
- 不新增、修改、停用任何排程、Startup 項目或 Run 機碼。
- 任何會起子程序的指令都不能閃黑窗：Python 內的 `subprocess` 一律帶 `creationflags=subprocess.CREATE_NO_WINDOW`（Windows）；不要開新的 console 視窗。
- 不跑完整 test suite（會起很多子程序）。只跑本 spec 指定的測試，且在 Part A worktree 內跑測試前必須設 `$env:PYTHONPATH = "<worktree>\src"`，否則會 import 到 primary checkout 的 `aram_nn`。
- 不執行 `cswap`。
- 遇到規則沒涵蓋或判定不明的項目：不動、記錄、回報。寧可少做。

## 3. Part A：Tracked 結構

### 3.1 目標結構

```
.claude/  .codex/  .github/  .gitignore
README.md  AGENTS.md  CLAUDE.md  CONTRIBUTING.md
PRODUCT.md  MODEL.md  DESIGN.md  OPERATIONS.md
pyproject.toml
docs/        # generated，不動
documents/
  README.md            # documents/ 唯一索引
  architecture/
  guides/
  reports/             # YYYY-MM-DD-kebab-slug.md
  archive/
runbooks/
  README.md            # runbooks/ 唯一索引
  task-definitions/
scripts/               # 根目錄凍結；新檔只進子目錄
  launchers/
src/  tests/
```

消失的頂層項目：`notes/`、`PLAN.md`、`augment-category-editor.html`、`start_recommender.bat`。

### 3.2 搬移對照（一律用 `git mv`，保留歷史）

| 現在 | 之後 | 備註 |
|---|---|---|
| `PLAN.md` | `documents/archive/PLAN-2026-05-12.md` | 檔內標題已自稱 Historical Plan |
| `notes/agents/experiments-and-data.md` | `documents/guides/experiments-and-data.md` | 現行指南 |
| `notes/issue-16-analysis.md` | `documents/reports/<首次 commit 日>-issue-16-analysis.md` | 日期用 `git log --follow --diff-filter=A --format=%as -- notes/issue-16-analysis.md` |
| `notes/task-definitions/MayhemLCUWatchdogKeepalive.xml` | `runbooks/task-definitions/MayhemLCUWatchdogKeepalive.xml` | 同步改 `OPERATIONS.md` 的兩處引用（見 3.5） |
| `notes/archive/DESIGN.md` | `documents/archive/DESIGN.md` | |
| `notes/archive/PRODUCT.md` | `documents/archive/PRODUCT.md` | |
| `notes/archive/TODO.md` | `documents/archive/TODO.md` | |
| `notes/archive/article-cover-banners.md` | `documents/archive/article-cover-banners.md` | `.claude/skills/write-column/SKILL.md` 有相對連結 |
| `notes/archive/design-experiments/z7k-washoku.md` | `documents/archive/design-experiments/z7k-washoku.md` | |
| `notes/archive/MODEL_NOTES-2026-05-20.md` | `documents/archive/MODEL_NOTES-2026-05-20.md` | 合併目標，見 3.3 |
| `notes/archive/MODEL_NOTES.md` | 刪除（內容併入上一列） | 見 3.3 |
| `notes/README.md`、`notes/archive/README.md`、`documents/reports/README.md` | 刪除（內容併入 `documents/README.md`） | 見 3.4 |
| `documents/personal-data-audit.md` | `documents/reports/2026-05-15-personal-data-audit.md` | 快照日 2026-05-15；若檔內日期不同以檔內為準 |
| `documents/reports/augment-encounter-bound-2026-10-09.md` | `documents/reports/2026-10-09-augment-encounter-bound.md` | |
| `documents/reports/composition_analysis_handoff_2026_06_11.md` | `documents/reports/2026-06-11-composition-analysis-handoff.md` | |
| `documents/reports/crawler-throughput-2026-10-15.md` | `documents/reports/2026-10-15-crawler-throughput.md` | |
| `documents/reports/final_project_report_2026_06_14.md` | `documents/reports/2026-06-14-final-project-report.md` | 現行 final |
| `documents/reports/final_project_text_report_2026_05_31.md` | `documents/archive/2026-05-31-final-project-text-report.md` | 被 06-14 版取代；檔頭加 `> Superseded by ../reports/2026-06-14-final-project-report.md` |
| `documents/reports/recommender_release_2026_10_09.md` | `documents/reports/2026-10-09-recommender-release.md` | |
| `documents/reports/search-discovery-2026-10-03.md` | `documents/reports/2026-10-03-search-discovery.md` | |
| `documents/reports/site_redesign_handoff_2026_06_28.md` | `documents/archive/2026-06-28-site-redesign-handoff.md` | 已被 `DESIGN.md` 與後續 nav redesign 取代；檔頭加 `> Superseded by ../../DESIGN.md` |
| `documents/reports/workspace-consolidation-2026-09-30.md` | `documents/reports/2026-09-30-workspace-consolidation.md` | |
| `start_recommender.bat` | `scripts/launchers/start_recommender.bat` | 見 3.6 |
| `augment-category-editor.html` | `git rm`（不再 track） | 見 3.6 |

歸檔前先讀內容確認「被取代」的判定成立：`final_project_text_report_2026_05_31.md` 若含有 06-14 版沒有的實質段落，把那些段落的標題列在執行紀錄，但仍照表歸檔（不合併內文）。

搬完後 `notes/` 不應再有任何 tracked 檔案。

### 3.3 合併 `MODEL_NOTES`

`MODEL_NOTES-2026-05-20.md` 是完整版（27 KB）；`MODEL_NOTES.md`（20 KB）是較短的舊版，只多了檔頭的 archived banner 與少數幾行。

1. `git diff --no-index notes/archive/MODEL_NOTES-2026-05-20.md notes/archive/MODEL_NOTES.md` 列出所有 `+` 行（只存在於 `MODEL_NOTES.md` 的內容）。
2. 把 banner（`> **Archived / superseded:** …`）放到合併檔最上方，並把 banner 內的相對連結改成新位置可用的路徑（`../../MODEL.md`；`PLAN.md`／`TODO.md` 改指 `PLAN-2026-05-12.md`／`TODO.md`）。
3. 其餘 `+` 行逐行判斷：內容已在完整版中出現（即使措辭不同）就略過；確實沒有的，加到合併檔末尾的 `## Lines carried over from MODEL_NOTES.md` 小節。
4. 刪除 `MODEL_NOTES.md`，把所有引用改指 `documents/archive/MODEL_NOTES-2026-05-20.md`。
5. 執行紀錄列出每一條 `+` 行的處置（略過／帶入）。

### 3.4 索引

- `documents/README.md` 改寫成唯一索引，四個小節：Architecture、Guides、Reports（依日期新到舊）、Archive。每個檔案一行：相對連結＋一句用途。保留原本三份 README 中仍然有效的說明文字（例如 archive 的閱讀注意事項），刪掉重複的。
- `documents/reports/README.md` 目前引用的 `augment_sequence_ml_handoff_2026_08_12.md` 不在 tracked 檔案中：確認後從索引移除，並在執行紀錄註明。
- `runbooks/README.md` 列出 `runbooks/` 下全部 runbook（目前漏了 `player-history.md`、`desktop-release.md`），加入 `repo-hygiene.md` 與 `task-definitions/` 的一行說明。
- 本 spec 與 `runbooks/repo-hygiene.md` 已經存在於 branch 的 working tree（未 commit），一併加入索引並 commit。

### 3.5 引用更新

搬移後跑下列搜尋，結果必須只剩「刻意保留的歷史敘述」（本 spec、`2026-09-30-workspace-consolidation.md` 這類描述過去狀態的報告內文可以保留舊路徑，但要是純文字而不是壞掉的連結）：

```powershell
git grep -n -E "notes/|notes\\\\|PLAN\.md|MODEL_NOTES\.md|personal-data-audit\.md|augment-category-editor\.html|start_recommender\.bat" -- . ":!docs"
git grep -n -E "(_20[0-9]{2}_[0-9]{2}_[0-9]{2}|-20[0-9]{2}-[0-9]{2}-[0-9]{2})\.md" -- . ":!docs"
```

已知需要更新的位置（行號是 `origin/main` 6fc5c659c2 當下的，僅供定位）：

| 檔案 | 位置 |
|---|---|
| `AGENTS.md` | L43、L86、L105（`notes/agents/experiments-and-data.md`）、L109（`PLAN.md`、`notes/archive/`） |
| `MODEL.md` | L12、L13、L58、L74、L77 |
| `OPERATIONS.md` | L42、L149（task XML 路徑，含 `schtasks /create … /xml notes\task-definitions\…`）、L158、L166 |
| `README.md` | L74（`notes/archive/PRODUCT.md`） |
| `CLAUDE.md` | 「手寫 Markdown 放根目錄、`notes/`、`documents/` 或 `runbooks/`」→ 移除 `notes/` |
| `.claude/skills/write-column/SKILL.md` | L22 相對連結 |
| `scripts/README.md` | L29、L42 |
| `documents/guides/experiments-and-data.md` | 原 L67，及所有因為搬了一層而失效的相對連結 |
| 被搬移的每一份文件 | 檔內相對連結重新計算 |

另外：

- `AGENTS.md`：在放置規則那一行（原 L71）補上「放置與整理規則見 `runbooks/repo-hygiene.md`」，並在它列 runbook／scoped rule 的地方加一行指向 `runbooks/repo-hygiene.md`。遵守該檔自己的「How to edit this file」（行數上限、頂端 line count 等）。`CLAUDE.md` 同理（60 行上限）。
- 新增 `.claude/skills/repo-hygiene/SKILL.md`：只做 routing（「整理專案、清 worktree、檔案該放哪」→ 讀 `runbooks/repo-hygiene.md`），格式比照既有的 `.claude/skills/deploy-shell/SKILL.md`，不複製 runbook 內容；在 `CLAUDE.md` 的 Scoped Rules 加一行。
- `OPERATIONS.md` 與 `runbooks/crawler-stall.md` 在 primary checkout 有別人未 commit 的修改。對這兩個檔只做路徑字串替換，不重排、不改其他字，並放在**獨立的最後一個 commit**，方便之後整合。
- `docs/` 是生成產物，不搜尋、不修改。

連結檢查：對所有 tracked、非 `docs/` 的 `.md`，解析 `[text](relative/path)` 形式的相對連結（忽略 `http`、`#anchor`、`mailto`），目標檔案必須存在。這個檢查寫進 `tests/test_repo_layout.py`（見 3.8）。

### 3.6 兩個頂層檔案

**`start_recommender.bat` → `scripts/launchers/start_recommender.bat`**

- 檔內 `cd /d "%~dp0"` 改成 `cd /d "%~dp0..\.."`，其餘不變（保留 `pythonw.exe`，不可改成會開 console 的寫法）。註解中的用法路徑一併更新。
- 這個檔原本編碼含非 ASCII 字元（破折號）；用原編碼位元組搬移，只改必要的那幾行，不要整檔重存成別的編碼。改完用 `git diff` 確認只有預期的行變動。
- 只讀掃描外部捷徑是否指向舊路徑，結果寫進執行紀錄（不修改捷徑）：`$env:USERPROFILE\Desktop`、`$env:APPDATA\Microsoft\Windows\Start Menu`、`$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar` 下的 `*.lnk`，用 `WScript.Shell` 的 `CreateShortcut(<lnk>).TargetPath`／`Arguments` 比對 `start_recommender.bat`。

**`augment-category-editor.html`**

- `scripts/build_augment_category_editor.py` 的預設輸出（L541 `default=Path("augment-category-editor.html")`）改成 `Path("outputs/exports/augment-category-editor.html")`，寫檔前 `mkdir(parents=True, exist_ok=True)`；docstring（L13）同步。
- `scripts/build_item_role_annotator.py` L562 的 `ROOT / "exports" / …` 改成 `ROOT / "outputs" / "exports" / …`，L6 註解同步，同樣確保目錄存在。
- `git rm augment-category-editor.html`。
- 搜尋 `scripts/ src/ tests/ runbooks/ .claude/ .codex/` 與 root `*.md` 中其他讀寫 `exports/` 或該 HTML 的地方並一併更新。

### 3.7 `.gitignore`

- `documents/` 區塊改成允許新結構：`documents/README.md`、`documents/architecture/**/*.md`、`documents/guides/**/*.md`、`documents/reports/*.md`、`documents/archive/**/*.md`；其餘維持 ignored（slide workspace、匯出檔、`documents/reports/_attachments/`）。移除 `!documents/personal-data-audit.md`。
- 移除 `exports/` 那一行與註解（已被 `outputs/` 涵蓋）。
- 確認 `runbooks/task-definitions/*.xml` 沒有被任何規則忽略。
- 驗證：`git status --short` 不應出現非預期的 untracked 檔；`git check-ignore -v` 抽查上述每一類各一個路徑。

### 3.8 檢查工具

**`src/aram_nn/repo_audit.py`**（只用標準函式庫；所有 `subprocess` 呼叫在 Windows 帶 `CREATE_NO_WINDOW`；全程只讀）

純函式（輸入是路徑字串清單，方便單元測試）＋一個 CLI：

```
python -m aram_nn.repo_audit [--repo PATH] [--workspace PATH] [--json]
```

- `--repo` 預設為此模組所在的 checkout；`--workspace` 預設為 `<repo 的上層>\aram-winrate-nn-workspace`。
- Exit code：有任何 FAIL 為 1，否則 0。

FAIL 檢查（對 `git ls-files` 的結果）：

| ID | 規則 |
|---|---|
| T1 | Tracked 頂層項目 ⊆ allowlist：`.claude .codex .github .gitignore AGENTS.md CLAUDE.md CONTRIBUTING.md DESIGN.md MODEL.md OPERATIONS.md PRODUCT.md README.md docs documents pyproject.toml uv.lock runbooks scripts src tests` |
| T2 | 直接位於 `scripts/` 下的 tracked 檔 ⊆ baseline 清單；`scripts/` 的 tracked 子目錄 ⊆ `templates site_data launchers research ops site` |
| T3 | Tracked `.md`（`docs/` 除外）只能在：root allowlist、`documents/`、`runbooks/`、`.claude/`、`.codex/`、`.github/`，或檔名為 `README.md` |
| T4 | `documents/reports/*.md` 檔名符合 `^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*\.md$` |
| T5 | `documents/` 下直接的 tracked 檔只有 `README.md`；tracked 子目錄 ⊆ `architecture guides reports archive` |
| T6 | `documents/` 下每個 tracked `.md`（`README.md` 除外）的相對路徑出現在 `documents/README.md`；`runbooks/*.md`（`README.md` 除外）出現在 `runbooks/README.md` |
| T7 | Tracked、非 `docs/` 的 `.md` 內相對連結目標存在 |

T2 的 baseline 存成資料檔（位置比照 `tests/` 既有的 fixture 慣例；若沒有慣例就放 `tests/fixtures/scripts_root_baseline.txt`），內容＝目前 `scripts/` 根目錄全部 122 個 tracked 檔，再加一行 `build_champion_icons.py`（另一個 session 進行中、尚未 commit 的檔案，預先放行）。`repo_audit` 與 test 讀同一份檔。失敗訊息要說明怎麼修：把新檔移到子目錄，或在有理由時更新 baseline 與 `runbooks/repo-hygiene.md`。

WARN／INFO 檢查（對本機狀態；對應 `runbooks/repo-hygiene.md` 的 Audit 一節）：

| ID | 規則 |
|---|---|
| L1 | Repo 頂層存在、但既非 tracked 也不在本機 allowlist（`data models logs outputs manual overwolf .venv dist build .git .claude .codex .impeccable .pytest_cache`）的項目 |
| L2 | `outputs/` 根目錄的檔案數 |
| W1 | `git worktree list --porcelain` 的每個 worktree 分類：protected（primary、api-runtime、publisher temp）／active／retire-candidate／merged-with-untracked／content-merged／keep，判定方式見 §4.2；另列出不在 `<workspace>\worktrees\` 下的 |
| W2 | 是 `origin/main` ancestor 且沒有 worktree 的 local branch |
| W3 | `<workspace>\backups\` 下超過 60 天的項目；`<workspace>\trash\` 下超過 14 天的項目 |
| S1 | `logs outputs .codex .claude` 與 workspace 各子目錄的大小、D 槽剩餘空間。**不要**遞迴走訪 `data\`、`models\`、`.venv\`、`.git\`、`node_modules\` |

走訪檔案系統時不得進入 reparse point：用 `os.scandir`，以 `entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT` 判斷並跳過。提供一個 `find_reparse_points(root)` 函式回傳 `(path, target, target_is_inside_root)`。

**`tests/test_repo_layout.py`**

- 對真實 repo 跑 T1–T7（用 `git ls-files`；不是 Git checkout 或找不到 `git` 時 skip）。
- 對純函式的單元測試：每條規則至少一個通過與一個失敗的合成案例；`find_reparse_points` 用 `tmp_path` 建一個目錄 junction 測試（建不出來就 skip），確認不會走進連結。
- 不得起任何會開視窗的子程序。

### 3.9 Part A 驗證

在 worktree 內：

```powershell
$wt = "D:\Projects\CODING\aram-winrate-nn-workspace\worktrees\repo-reorg"
$env:PYTHONPATH = "$wt\src"
& "D:\Projects\CODING\aram-winrate-nn\.venv\Scripts\python.exe" -m pytest "$wt\tests\test_repo_layout.py" -q
& "D:\Projects\CODING\aram-winrate-nn\.venv\Scripts\python.exe" -m aram_nn.repo_audit --repo $wt
git -C $wt ls-tree --name-only HEAD
git -C $wt diff --stat origin/main...HEAD -- docs
```

- Layout test 全過；`repo_audit` FAIL=0。
- `ls-tree` 頂層剛好是 §3.1 的 19 項（`uv.lock` 目前未 tracked，不會出現）。
- `docs/` 的 diff 為空。
- 既有測試中會讀到被搬移路徑的（`git grep -l -E "notes/|documents/|PLAN\.md|exports" -- tests`）逐一用同樣的 `PYTHONPATH` 跑過；只跑這些，不跑整個 suite。
- `python -c "import ast,sys; [ast.parse(open(p,encoding='utf-8').read()) for p in sys.argv[1:]]"` 對兩個改過的 builder script 做語法檢查；不要實際執行 builder（需要 `data/`）。

### 3.10 Commit 切法

每個 commit 前 `git status --short`、`git diff --cached --name-only`、`git diff --cached`；只 `git add` 明確路徑。

1. `Docs: add repo hygiene runbook and reorganization spec`
2. `Docs: fold notes/ and PLAN.md into documents/, normalize report names`（純搬移＋索引＋引用更新，不含 `OPERATIONS.md`）
3. `Repo: move launcher and tool exports off the top level`（`.bat`、兩個 builder、`.gitignore`）
4. `Repo: add layout test and repo_audit CLI`
5. `Docs: point OPERATIONS at the new task-definition path`（只有 `OPERATIONS.md`／`runbooks/crawler-stall.md` 的路徑替換）
6. `Docs: record reorganization execution log`（§8 的 Part A 段落）

Commit message 的 attribution 行依執行 session 自己的規則。不 push、不開 PR、不 merge。

## 4. Part B：Worktree 與 branch

所有 Git 指令用 `git -C D:\Projects\CODING\aram-winrate-nn <cmd>` 形式，且只限：`fetch --prune origin`、`worktree list|remove|move|prune`、`branch --list|-d`、`bundle create`、`rev-parse`、`merge-base`、`merge-tree`、`log`、`status`、`for-each-ref`、`ls-files`。對個別 worktree 的 `status`／`ls-files` 用 `git -C <worktree>`。

### 4.1 備份（任何刪除之前）

備份目錄：`D:\Projects\CODING\aram-winrate-nn-workspace\backups\reorg-20261110\`。

1. 確認 D 槽剩餘空間 ≥ 20 GB，否則停止 Part B 並回報。
2. `git fetch --prune origin`。
3. `git bundle create <備份目錄>\all-refs.bundle --all`，再 `git bundle verify`。
4. `worktrees-before.txt`：`git worktree list --porcelain` 原文。
5. `branches-before.txt`：`git for-each-ref --format="%(refname) %(objectname) %(committerdate:iso8601)" refs/heads refs/remotes`。
6. 每個 worktree 一份 `status\<name>.txt`：HEAD sha、branch、`git status --short` 原文。

### 4.2 分類規則

對 `git worktree list --porcelain` 的每一個 worktree 依序判定，第一個符合的就是結果：

| 順序 | 類別 | 條件 | 動作 |
|---|---|---|---|
| 1 | Protected | Primary；`aram-winrate-nn-api-runtime`；路徑含 `arammeta-publish-`；`repo-reorg` | 不動 |
| 2 | External link | `find_reparse_points`（或等效掃描，不進入連結）發現指向 worktree 外部的 reparse point | 不動，回報連結與目標 |
| 3 | In use | 有 process 的 command line 或 executable path 含該 worktree 路徑（`Get-CimInstance Win32_Process`） | 不動 |
| 4 | Active | 最近活動 < 7 天（即 2026-11-03 之後）。最近活動＝下列最大值：HEAD commit 日期、該 worktree gitdir 內 `index` 與 `HEAD` 的 mtime、`git status --short` 列出的每個檔案的 mtime | 不動 |
| 5 | Dirty | `git status --short` 有 `??` 以外的任何項目 | 不動，回報 |
| 6 | Merged | `git merge-base --is-ancestor <HEAD> origin/main` exit 0 | 退役（4.3） |
| 7 | Content-merged | `git merge-tree --write-tree origin/main <HEAD>` 成功且輸出的 tree 等於 `git rev-parse origin/main^{tree}`，且 `git bundle verify` 已通過 | 退役 worktree；**保留 branch** |
| 8 | Unmerged | 其餘 | 不動，列出 ahead 數與最後 commit 日期 |

Part A 的 `repo_audit` 可能尚未完成；Part B 自己實作等效掃描（Python `os.scandir` 加 `FILE_ATTRIBUTE_REPARSE_POINT`，不進入連結），腳本放在備份目錄內。

2026-11-10 盤點時的預期（僅供對照；以規則的實際判定為準，不一致時在紀錄中說明）：

- Active：`aram-wt-writer-batch1`、`aram-wt-id-gap-backfill`、`.claude\worktrees\aug-unclassified-chips`、`id-gap-sweep`、`orchid-theme`、`riot-ux-close`、`seed-requeue-guard`、`sgp-history`。
- Merged、clean：`aug-show-all`、`aug-tip-click-only`、`blitz-description-hover`、`current-draft-splash`、`language-button-label`、`new-patch-augment-descriptions`、`quick-overview-tabs`、`recommender-site-tabs`、`.claude\worktrees\header-role-search`、`D:\Projects\CODING\aram-wt-augslot`。
- Merged、有 untracked：`D:\Projects\CODING\aram-destiny-rec`（1）、`champion-difficulty-evidence`（34）、`champion-indexable-content`（554）、`skill-coefficient-clarity`（3）、`yellow-ability-radar`（1）。
- Clean、ahead of `origin/main`（要用第 7 列判定）：`D:\Projects\CODING\aram-exe-trust`、`augment-weight-expansion`、`champ-page-default-overview`、`crawler-maintenance-guard`、`hide-game-nav`、`publisher-performance`、`stop-classic-crawl`。

### 4.3 退役步驟（每個 worktree）

1. 重新跑一次 `git -C <wt> status --short`（防止分類後有變動）。
2. 有 untracked 檔（`??`）：
   - 用 `git -C <wt> ls-files --others --exclude-standard` 取得清單並計算總大小。超過 500 MB → 不退役，回報。
   - 逐檔**搬移**到 `<備份目錄>\untracked\<worktree 名>\<相對路徑>`（同磁碟搬移；建立所需的父目錄）。不搬 ignored 檔。
   - 再跑 `status --short`，必須無輸出。
3. `git -C D:\Projects\CODING\aram-winrate-nn worktree remove <path>`（不加 `--force`）。失敗就保留、記錄錯誤訊息、繼續下一個。
4. 全部做完後 `git worktree prune`，再存 `worktrees-after.txt`。

`git worktree remove` 之後若目錄仍殘留（例如被鎖住的檔案），不要手動遞迴刪除；記錄並回報。

### 4.4 搬移位置不合規的 worktree

退役後仍存在、路徑不在 `aram-winrate-nn-workspace\worktrees\` 下、而且類別不是 Protected／External link／In use／Active 的 worktree：`git worktree move <old> D:\Projects\CODING\aram-winrate-nn-workspace\worktrees\<原目錄名去掉 aram- 與 wt- 前綴>`。目標已存在就不搬並回報。`.claude\worktrees\` 下的不搬（由 Claude Code 管理）。

### 4.5 Local branch

- 對每個 local branch：若是 `origin/main` 的 ancestor、沒有任何 worktree checkout 它、而且不是 `main` → `git branch -d <name>`。`-d` 拒絕就保留並記錄。
- 其餘 local branch 與所有 remote branch：不刪，在紀錄中列表（名稱、ahead／behind、最後 commit 日期、是否有 worktree），供 owner 決定。

## 5. Part B：本機 ignored 檔

隔離區：`D:\Projects\CODING\aram-winrate-nn-workspace\trash\2026-11-10\`，結構為 `<來源標籤>\<原始相對路徑>`，來源標籤用 `primary` 或 `workspace`。隔離一律是同磁碟搬移（`Move-Item`／`os.replace`），並在隔離區根目錄維護 `MANIFEST.md`（原路徑、新路徑、大小、最新 mtime、還原指令）。

每個項目動手前的共同檢查：路徑不是 reparse point；不在 `git -C <primary> status --short` 與 `git ls-files` 的結果中；沒有 process 開著（搬移失敗就視為使用中，保留並記錄）。

### 5.1 空目錄移除

確認存在、為空、不是 reparse point 後用 `[System.IO.Directory]::Delete($path, $false)`：

- `D:\Projects\CODING\aram-winrate-nn\.tmp-sqlite`、`D:\Projects\CODING\aram-winrate-nn\.tmp_sqlite` —— 先在 primary checkout（含 untracked 的 `src\aram_nn\lcu\maintenance.py`）搜尋 `tmp-sqlite|tmp_sqlite`；有程式引用就保留並記錄。
- `D:\Projects\CODING\aram-wt`
- `D:\Projects\CODING\aram-winrate-nn-workspace\deploy`
- `D:\Projects\CODING\aram-winrate-nn-workspace\worktrees\icon-fill-a` —— 先確認不在 `git worktree list` 中。

### 5.2 `outputs\` 根目錄散檔

對 `outputs\` 根目錄的每個**檔案**（不含子目錄）：

- mtime 在 7 天內 → 保留。
- 檔名（basename）出現在下列任一處 → 保留並記錄引用位置：primary checkout 的 tracked 檔（`git grep -F`，排除 `docs/`）、primary 的 untracked WIP 檔、`.claude\skills\`、`.codex\skills\`、`C:\Users\柯子捷\.claude\skills\`、`C:\Users\柯子捷\.claude\projects\D--Projects-CODING-aram-winrate-nn\memory\`。
- 其餘 → 搬到 `outputs\_archive\2026-11-10-loose\`（`_probe_*.py`、`_patch_*.py` 這類 scratch script 放 `scratch-scripts\` 子目錄，其他放 `files\`）。這是 repo 內歸位，不是隔離。

### 5.3 歸位

- `exports\*` → `outputs\exports\`（同名衝突時保留較新的，舊的加 `.old` 後綴）；之後 `exports\` 為空則依 5.1 的方式移除。`exports\_probe_malz.py` 歸到 `outputs\_archive\2026-11-10-loose\scratch-scripts\`。
- Root 的 `augment-category-editor.html` 在 primary 仍是 tracked 檔：**複製**一份到 `outputs\exports\augment-category-editor.html`，不動原檔。
- `documents\reports\` 下的非 `.md` 檔（ignored 附件）→ `documents\reports\_attachments\`。先在 primary 的 tracked `.md` 搜尋這些檔名；有連結引用的列在紀錄中（Part A 不會修這些連結，留給整合時處理）。

### 5.4 隔離

符合共同檢查，且目錄內最新檔案的 mtime 早於 2026-10-11（30 天）才隔離；否則保留並記錄。

| 項目 | 備註 |
|---|---|
| `graphify-out\` | 可重建 |
| `artifacts\` | 8 月的 visual-QA PNG |
| `logs\` 內 mtime 早於 2026-10-11 的**檔案** | 只搬檔案，不搬 `logs\` 本身；搬不動的（使用中）跳過 |
| `outputs\six_axis_reel\`、`outputs\six_axis_reel_claude\`、`outputs\reel_cache\` | 影片 render 與快取。隔離前在紀錄列出其中深度 ≤ 2 的 `*.mp4`（路徑、大小），方便 owner 撈回成品 |
| `outputs\marker-preview\`、`outputs\preview-augch\`、`outputs\video_edit_2026-07-15\` | Preview／剪輯暫存 |

隔離前同樣做 5.2 的引用搜尋（用目錄名）；被 tracked 程式或 skill 引用為**輸入**的目錄不隔離。不要隔離上表以外的 `outputs\` 子目錄（它們可能是 model lineage）。

### 5.5 只列候選、不動

在紀錄中列表（路徑、大小、最新 mtime、建議）：

- `data\` 內的舊 log、5 月的 parquet snapshot、`data\models\` 8 月的 `.pt`（lineage，owner 決定）。
- `.codex\logs\`（約 811 MB）、`.codex\tools\`（工具狀態）。
- `documents\open-slide-ethereal-items\`、`documents\open-slide-final-project\`（含 `node_modules`）、`documents\exports\`、`documents\pptx_export\` —— 應搬到 sibling workspace，但 `.claude\launch.json` 引用它們且該檔在 primary 有未 commit 修改，這次不搬。
- `workspace\backups\` 下各項目與 `workspace\history-rewrite-306e739e\` 的日期與大小；超過 60 天的標示。
- Primary 頂層未 tracked 的 `uv.lock`（應決定 track 或 ignore）、`.claude\settings.local.json.doctor-bak`。
- `outputs\` 中大於 100 MB 的其他子目錄。

### 5.6 Part B 驗證與紀錄

- `git -C D:\Projects\CODING\aram-winrate-nn status --short` 的輸出在 Part B 前後必須完全相同（開始時存 `primary-status-before.txt`，結束時比對）。
- `git worktree list --porcelain` 中 Protected 與 Active 的 worktree 全部還在。
- `Test-Path D:\Projects\CODING\aram-winrate-nn\data\lcu\games.db` 為真；`models\` 下的目錄清單前後相同。
- `D:\Projects\CODING\arammeta` 仍是 junction 且目標不變。
- 寫 `D:\Projects\CODING\aram-winrate-nn-workspace\backups\reorg-20261110\execution-log.md`：
  - Worktree 表：名稱、路徑、分類、判定依據、動作、結果。
  - Branch 表：刪除的、保留的（附原因）、remote 候選。
  - 本機檔案表：每個項目的動作（移除／歸位／隔離／保留）、大小、原因。
  - 候選清單（5.5）。
  - 前後對照：worktree 數、local branch 數、primary 頂層項目數、`outputs\` 根目錄檔案數、隔離區總大小、D 槽剩餘空間。
  - 所有被跳過或失敗的項目與原因。

## 6. 驗收條件

Part A：

- [ ] §3.1 的 tracked 頂層 19 項，`notes/`、`PLAN.md`、`augment-category-editor.html`、`start_recommender.bat` 不在頂層。
- [ ] §3.2 每一列都完成，且 `git log --follow` 能追到搬移前的歷史。
- [ ] `MODEL_NOTES` 只剩一份，`+` 行處置有紀錄。
- [ ] `documents/` 與 `runbooks/` 各只有一份索引，且列出所有檔案。
- [ ] §3.5 的兩個 `git grep` 沒有壞掉的引用。
- [ ] `tests/test_repo_layout.py` 全過；`python -m aram_nn.repo_audit --repo <worktree>` FAIL=0。
- [ ] `docs/` 無 diff；`OPERATIONS.md`／`runbooks/crawler-stall.md` 的 diff 只有路徑字串。
- [ ] Commits 照 §3.10；沒有 push。

Part B：

- [ ] `all-refs.bundle` 通過 `git bundle verify`。
- [ ] 每個 worktree 都有分類與判定依據；被退役的都符合 §4.2 第 6 或第 7 列。
- [ ] Primary 的 `git status --short` 前後相同；`games.db`、`models\`、`arammeta` junction 不變。
- [ ] 沒有任何永久刪除，除了 §5.1 的空目錄、`git worktree remove`、`git branch -d`。
- [ ] `MANIFEST.md` 與 `execution-log.md` 完整。

## 7. 這次不做的事與原因

| 項目 | 原因 | 之後怎麼辦 |
|---|---|---|
| 實體搬移 `scripts/` 根目錄的 122 個檔案 | `src/` 的 model-refresh pipeline 以檔名呼叫其中的「research」script；幾乎每個 script 都用 `__file__` 推 repo root；publisher 從 `origin/main` build 而 live 程式跑在 primary；排程與 repo 外的捷徑／launcher 直接引用路徑；primary 上有進行中的 WIP | 先凍結根目錄（T2）。要搬就另開 branch，一次一個分類，先把 hub module 收進 `src/aram_nn/` |
| `data/`、`models/` 內的清理 | Lineage 與 live 資料；`games.db` 所在目錄風險最高 | Audit 列出，owner 決定 |
| Slide workspace 搬到 sibling workspace | 需要改 `.claude/launch.json`，該檔在 primary 有未 commit 修改 | WIP 落地後再搬 |
| 刪除 unmerged／remote branch | 可能還需要 rollback | 紀錄列候選，owner 決定 |
| 永久刪除隔離區 | 不可逆 | Owner 確認後才刪；至少保留 14 天 |
| 新增 CI workflow | Publisher 頻繁直接 push `main`；未 push 無法驗證 workflow | 之後若要，在 PR 上加一個只跑 `tests/test_repo_layout.py` 的 workflow |

## 8. 執行紀錄

### Part A

執行者：Claude Code（Sonnet 5.5）派工 session，2026-11-10。Branch `codex/repo-reorg`，從 `origin/main` 6fc5c659c2 開；沒有 push、沒有開 PR、沒有 merge。

#### Commits

| # | sha | 摘要 | 檔案數 |
|---|---|---|---|
| 1 | `a626bf0191` | Docs: add repo hygiene runbook and reorganization spec | 2 |
| 2 | `6929d3ba00` | Docs: fold notes/ and PLAN.md into documents/, normalize report names（含 `.gitignore` 的 `documents/` 區塊，見偏離 1） | 34 |
| 3 | `d3e1288d95` | Repo: move launcher and tool exports off the top level | 7 |
| 4 | `5bf8dd96d4` | Repo: add layout test and repo_audit CLI | 3 |
| 5 | `34178589b5` | Docs: point OPERATIONS at the new task-definition path（只有 `OPERATIONS.md` L42、L132、L141、L149 四行） | 1 |
| 6 | 本 commit | Docs: record reorganization execution log（只有本檔 §8 Part A） | 1 |

§3.2 的搬移都用 `git mv`／`git rm`。`git log --follow` 抽查可追到搬移前歷史：`documents/archive/PLAN-2026-05-12.md` 5 個 commit（最舊 2026-05-14）、`documents/archive/MODEL_NOTES-2026-05-20.md` 9 個、`scripts/launchers/start_recommender.bat` 3 個、`runbooks/task-definitions/` 下的 XML 3 個、`documents/guides/experiments-and-data.md` 3 個、`documents/reports/2026-08-10-issue-16-analysis.md` 3 個、`documents/reports/2026-05-15-personal-data-audit.md` 3 個。

#### `MODEL_NOTES` 的 `+` 行處置

完整版 → 舊版的 diff 中，舊版多出的行分三組：

| 舊版行 | 處置 |
|---|---|
| `Updated: 2026-05-18` | 略過：完整版是較新的 `Updated: 2026-05-20` |
| `> **Archived / superseded:** …` banner | 帶入：放在合併檔檔頭，連結改成 `../../MODEL.md`、`PLAN-2026-05-12.md`、`TODO.md`；完整版原有的 `Archived snapshot` banner 保留，其中 guides 連結改為 `../guides/experiments-and-data.md` |
| `When the user asks for a new ML round (…)` 一行 | 帶入檔尾 `## Lines carried over from MODEL_NOTES.md`：完整版對應句的觸發詞已是亂碼，舊版這行字元完整 |

#### `final_project_text_report_2026_05_31.md` 歸檔確認

照表歸檔，未合併內文；檔頭加 `Superseded by ../reports/2026-06-14-final-project-report.md`。05-31 版有、06-14 版沒有同名標題的段落：`1. Introduction`、`2. Dataset`、`3. Methods`（含 3.1–3.4）、`4. Evaluation Metrics`、`5. Results`、`6. Discussion`、`7. Limitations`、`8. Conclusion`、`Academic Integrity Note`，以及 H1 標題。06-14 版是重新編排章節（`3. Representation and Models`、`4. Headline Benchmark` 等），不是同名刪除；未發現需要搶救的獨有段落，但沒有逐段比對內容。

#### 移除的失效索引項

舊 `documents/reports/README.md` 引用 `augment_sequence_ml_handoff_2026_08_12.md`，`git ls-files` 中沒有這個檔案，新 `documents/README.md` 已省略。

#### 外部捷徑掃描（唯讀）

用 `WScript.Shell.CreateShortcut` 掃使用者的 `.lnk`，0 筆指向 `start_recommender`；未修改任何捷徑。

#### 驗證輸出（worktree 內，`PYTHONPATH=<worktree>\src`）

```
pytest tests/test_repo_layout.py -q            -> 20 passed
python -m aram_nn.repo_audit --repo .          -> FAIL=0 WARN=4 INFO=34 (exit 0)
git ls-tree --name-only HEAD                   -> 19 項：.claude .codex .github .gitignore AGENTS.md CLAUDE.md CONTRIBUTING.md DESIGN.md MODEL.md OPERATIONS.md PRODUCT.md README.md docs documents pyproject.toml runbooks scripts src tests
git diff --name-only origin/main...HEAD -- docs -> 空
ast.parse(build_augment_category_editor.py, build_item_role_annotator.py) -> ok
```

`repo_audit` 的 4 筆 WARN 都是 W3：3 個 `pre-claude-rewrite` bundle（90 天）與 `single-writer-cutover-20260827_215155`（75 天），超過 60 天門檻，留給 owner 決定。§3.5 的 `git grep` 剩餘命中只有本 spec、新檔名、`documents/README.md` 與合併檔內對舊檔名的說明文字、`tests/test_repo_layout.py` 的合成字串；T7 沒有壞掉的相對連結。`tests/test_player_history_transform.py` 只因測試名稱含 "exports" 而命中，與路徑無關，未執行。

#### 偏離 spec 之處

1. **`.gitignore` 的 `documents/` 區塊提前到 commit 2**：原定 commit 3，但 `documents/archive/` 與 `documents/guides/` 在舊規則下是 ignored，新位置檔案無法 `git add`。`exports/` 那行與註解仍在 commit 3 移除。
2. **`MODEL_NOTES` banner 位置**：放在 H1／`Updated` 行之下，而不是 H1 之前；`Superseded by` 標記同樣在 H1 之下。
3. **`runbooks/crawler-stall.md` 沒有改**：其中沒有舊路徑；commit 5 只含 `OPERATIONS.md`。
4. **`scripts/tierlist_render.py` 註解**：只改註解中的 `exports/item-role-annotator.html` 為 `outputs/exports/…`，屬 spec 要求的其他引用處更新；程式行為沒變。
5. **`repo_audit` 未實作 W1 的 `In use` 行程掃描**：需要起子程序，改輸出一行 INFO，提醒退役前人工確認。T7 不檢查 code span／fenced block 內的連結範例；預設 `--workspace` 在 task worktree 內會解析成該 workspace；連結目標是否存在以 tracked 檔案集合判定。
6. **只新增 `.claude/skills/repo-hygiene`**：`.codex/skills/` 沒有對應 adapter（該目錄目前只有 `crawler-stall-response`、`opgg-seed-refresh`、`update-mayhem-site`）。
7. **`runbooks/repo-hygiene.md` 沒有修改**：實作的檢查與它的 Audit 一節一致。

#### 需要 owner 決定的事項

- `.codex/skills/` 是否也要有 `repo-hygiene` adapter。
- 4 筆 W3 舊備份（見上）要保留、隔離或刪除。
- 合併後的 `MODEL_NOTES-2026-05-20.md` 內文仍有一行原本就存在的亂碼，未修改。
- `repo_audit` 是否要補 `In use` 行程檢查（需 Win32_Process 掃描）。
- 整合到 `main` 時，`OPERATIONS.md` 若與 primary 的未提交修改衝突：commit 5 只動四行，方便手動合併。

### Part B

（由 `backups\reorg-20261110\execution-log.md` 摘要併入。）
