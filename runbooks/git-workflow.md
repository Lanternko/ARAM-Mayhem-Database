# Git and Worktree Workflow

## Purpose

這是 human／agent source change 的 canonical Git 與 worktree SOP。它決定何時建立或重用 worktree、何時 fetch／pull／rebase／merge／push，以及何時可以安全清理；`AGENTS.md` 只保存不可違反原則，`OPERATIONS.md` 只保存 live harness ownership。

網站 routine data publisher 是唯一自動化特例：精確行為由 `runbooks/site-deploy.md` 與 `src/aram_nn/site/static_publish.py` 擁有，不用一般 task branch 模擬。Frontend shell 與 generator／schema source change 仍走一般 task worktree。

## Worktree classes

| 類型 | 用途 | Branch／生命週期 | Owner |
|---|---|---|---|
| Primary checkout | `main`、live harness anchor、整合與觀測 | 長期存在；保持可辨識，不拿來堆新 feature WIP | Repository owner |
| Task worktree | 一個明確 source／docs／experiment change | 一個 `codex/<task>` branch；工作完成後驗證再清理 | 建立該 task 的 human／agent |
| Disposable automation worktree | 原子 build／publish | 從 `origin/main` detached 建立；單次結束即移除 | Harness only |

Primary checkout 會被本機 watchdog、scripts 與資料路徑使用；直接在這裡改 source 可能讓 live process 載入未完成程式。一般修改優先放 task worktree，而且 worktree path 必須在 repository 目錄之外，避免 nested repo 被工具或測試掃入。

## Non-negotiable safety gates

- 任何 Git mutation 前先跑 `git status --short` 與 `git worktree list --porcelain`；先知道 branch、path 與未提交 owner，才不會覆蓋別人的 WIP。
- Dirty worktree 不得自動 pull、rebase、merge、switch、remove 或 prune；先盤點 tracked、staged、untracked，再由 owner 決定 commit、保留或移轉。
- 不自動 stash。Stash 會隱藏 ownership，且常漏掉或混入 untracked files；只有明確決定內容與恢復位置後才可使用。
- Stage 明確 review 的 path；本 repo 不用 `git add .` 或 `git add -A`，因為 primary checkout 經常同時有 generated output、local state 與 unrelated WIP。
- 不使用 `git push --force` 或 `--force-with-lease`。需要改寫已發布歷史時，另開 replacement branch 或取得明確 owner 決策。
- 不用檔案總管或 `Remove-Item` 手動刪 worktree directory；Git metadata 必須經 `git worktree remove` 與 `git worktree prune` 維持一致。

## Inspect before work

在預計修改的 checkout 執行：

```powershell
git status --short --branch
git worktree list --porcelain
git fetch --prune origin
git log --oneline --decorate -5
```

`fetch` 只更新 remote-tracking refs，不改 working tree，因此開始、恢復、整合與 push 前都可以做。若發現目標 branch 已在其他 worktree、worktree dirty，或同一路徑有 live process，停在盤點階段，不建立第二份互相競爭的工作。

## Create one task worktree

從最新 `origin/main` 建立一個 branch 對應一個 task：

```powershell
$taskName = "short-task-name"
$taskBranch = "codex/$taskName"
$taskWorktree = "D:\Projects\CODING\aram-winrate-nn-$taskName"

git fetch --prune origin
git worktree add -b $taskBranch $taskWorktree origin/main
git -C $taskWorktree status --short --branch
```

Branch 已存在時不要加 `-b` 或另造近似名稱；先用 `git worktree list --porcelain` 找到它的 checkout。若 branch 存在但沒有 worktree，確認 ownership 後才用 `git worktree add $taskWorktree $taskBranch` 恢復。

Ignored runtime inputs 不會自動出現在 task worktree。不要因此移動 `games.db`／WAL／SHM；測試使用 fixture、snapshot、明確唯讀路徑，或由對應 harness 提供的 link mechanism。

## Choose how to update

| 狀態 | 動作 | 原因 |
|---|---|---|
| 只要知道 remote 是否有新 commit | `git fetch --prune origin` | 不改 working tree，任何乾淨或 dirty checkout 都安全 |
| Primary `main` clean，且只需跟上 remote | `git pull --ff-only origin main` | 只接受 fast-forward，不偷偷製造 merge commit |
| Primary `main` dirty | 只 fetch，不 pull | Pull 會把 upstream change 與未完成 WIP 疊在一起 |
| Task branch 尚未 push／沒人共用 | clean 後 `git rebase origin/main` | 保持 local history 線性，且不改寫他人已看見的 commit |
| Task branch 已 push 或有人共用 | clean 後 `git merge --no-edit origin/main` | 保留已發布 history，不需要 force push |
| Remote task branch 本身也前進 | 先 fetch，再 merge 該 remote branch | 明確吸收別人的 commit，不猜測 ownership |

Task worktree 不使用無參數 `git pull`；fetch 後依上表明確選 rebase 或 merge。遇到 conflict 要保留 conflict evidence 並停止自動整合；不為了讓指令通過而任選一側。

## Review, commit, and push

先驗證 source 與 scope，再 commit：

```powershell
git status --short
git diff --check
git diff --stat
git diff
git add path/to/reviewed-file another/reviewed-file
git diff --cached --name-only
git diff --cached
git commit -m "Describe the intentional change"
```

另外執行與修改範圍相稱的 tests／build／visual QA；精確命令以 `scripts/README.md`、目標 CLI `--help`、`DESIGN.md` 或對應 runbook 為準。

第一次發布 task branch：

```powershell
git push -u origin codex/short-task-name
```

後續只有在 relevant verification 通過、commit scope 沒有 DB、cache、log、credential 或 unrelated WIP 時才 `git push`。Push 被拒絕時回到 fetch＋rebase／merge decision，不 force push。

## Integrate into main

Source、docs、model 或 UI change 進 `main` 前必須完成 scope review 與相稱驗證。優先使用 reviewed PR；若 owner 明確選擇本機整合，也只能在 clean、已 fast-forward 到 `origin/main` 的 primary checkout 進行。`main` 已被 primary checkout 使用，不要嘗試在 task worktree 再 switch 到 `main`。

```powershell
git switch main
git pull --ff-only origin main
git merge --no-ff codex/short-task-name
git status --short
```

Merge 後重跑受影響驗證並 review merge diff，才可 `git push origin main`。Agent 不自行把 source branch merge 或 push 到 `main`；必須是使用者要求的 integration／publish scope。

Routine data publisher 是直接 push `main` 的 automation exception，但只能提交 canonical generated allowlist。它不得帶入 development source、primary checkout WIP 或 partial artifacts。

## Retire a worktree

Worktree cleanup 與 branch deletion 是兩個不同決策。先證明 worktree clean、沒有 live process 使用，而且 HEAD 已可從 `origin/main` 到達：

```powershell
$taskWorktree = "D:\Projects\CODING\aram-winrate-nn-short-task-name"
$taskBranch = "codex/short-task-name"

git fetch --prune origin
git -C $taskWorktree status --short
git merge-base --is-ancestor $taskBranch origin/main
git worktree remove $taskWorktree
git worktree prune
git worktree list --porcelain
```

`status --short` 必須沒有輸出，且 `merge-base --is-ancestor` 必須 exit 0。任何檢查失敗都保留 worktree 並記錄原因；不得用 `git worktree remove --force` 清掉不明修改。

Local／remote branch cleanup 只有在 owner 明確要求，且已確認不再需要 rollback 時才分開執行。不要因為移除 worktree 就順手刪 remote branch。

## Disposable publisher contract

Production publisher 的 executable contract 是：

1. Fetch `origin`，從最新 `origin/main` 建 detached temporary worktree。
2. Link 需要的 ignored local build inputs，但不複製或提交它們。
3. Build、stage 並 commit 唯一的 `DEFAULT_DOC_PATHS` allowlist。
4. Push 前再 fetch；若 remote 在 build 期間前進，丟棄本輪，下一 cycle 從新 HEAD 重建。
5. 只有 remote 沒前進時才 push `HEAD:main`；不 pull、不 rebase、不 force push。
6. Unlink inputs，移除 harness 自己建立的 temporary worktree，再 prune metadata。

`--main-worktree` 不是 production lane；它會失去 WIP isolation，只能在明確診斷情境使用。完整發布、failure routing 與 live verification 見 `runbooks/site-deploy.md`。

## Audit cadence

- 建立新 worktree 前：列出所有 worktrees，避免 branch／path 重複。
- Task merge 後：立即做 retire preflight；能安全清理才清理。
- 發現 temp／detached／數月未動 worktree：逐一查 status、HEAD 是否在 `origin/main`、是否有 live process；先分類 clean-merged、clean-unmerged、dirty，再決定，不批次強制刪除。
- Publisher 留下 temporary worktree：視為 failed cleanup，先查 publisher log 與 symlink state，不假設內容可刪。
