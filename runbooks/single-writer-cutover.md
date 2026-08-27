# Single-writer crawler cutover

把 live harness 從「兩個 `lcu_collector.py snowball` 直連 `games.db`」換成「一個 `snowball-workers` fleet：兩個 RPC producer + 一個 SQLite writer」。

這是操作 SOP，不是 Git merge。Primary checkout 是 live import path，而且現在是 dirty WIP；不要在 crawler 還在寫 DB 時 `git switch` / `git merge`。

## Preconditions

- Source of the new crawler: worktree
  `D:\Projects\CODING\aram-winrate-nn-single-db-writer-merge-20260827`
  branch `codex/single-db-writer-merge-20260827`（至少 `e49a7638`）。
- Live harness / DB: `D:\Projects\CODING\aram-winrate-nn`
  `data/lcu/games.db` 留在原地。不要複製、移動、或分開 WAL／SHM。
- Live DB 已有 `classic_rate_v1_bootstrap_done`；writer 第一次打開不應再掃 frontier。
- 單元測試已在 worktree 綠過。Cutover 的驗收是實機吞吐與 process 拓撲，不是再跑一次 pytest。
  worktree 的 `aram_nn` 不是 import 路徑（editable install 指向 primary），所以要用
  `PYTHONPATH=<worktree>\src python -m pytest tests/ -q` 才是真的在測新程式碼。
- **Pre-flight（crawler 還在跑時做完）。** 這兩個檢查不需要停機就能做，放在停機之後等於
  拿「crawler 已經死了」去換「發現依賴壞掉」：

  ```powershell
  $src = "D:\Projects\CODINGram-winrate-nn-single-db-writer-merge-20260827"
  $env:PYTHONPATH = "$src\src"
  python -c "from aram_nn.lcu.writer_service import WriterService; from aram_nn.lcu.writer_supervisor import WriterSupervisor; print('import-ok')"
  python "$src\scripts\lcu_collector.py" snowball-workers --help | Select-Object -First 3
  Remove-Item Env:PYTHONPATH
  ```
- 全程只有一個 writer 打 `games.db`。禁止並行：舊 `snowball`、新 `snowball-workers`、`lcu_collector.py collect`。

## Topology change

Before:

```text
mayhem_lcu_watchdog.py
├─ lcu_collector.py snowball  (W01, 直連 SQLite)
└─ lcu_collector.py snowball  (W02, 直連 SQLite)
```

After:

```text
mayhem_lcu_watchdog.py
└─ lcu_collector.py snowball-workers --workers 2
   ├─ aram-single-db-writer     (唯一 SQLite 寫入者)
   ├─ producer W01              (RPC, 不開 games.db)
   └─ producer W02              (RPC, 不開 games.db)
```

Watchdog 把「一個 `snowball-workers` process」當成 fleet。`--workers 2` 是 fleet 啟動時的 producer 數，不是兩個 supervisor。

## Files to overlay

從 worktree 覆寫到 primary checkout。只動 crawler runtime；不要帶 `docs/`、`data/`、generated site。

```
src/aram_nn/lcu/db_state.py
src/aram_nn/lcu/poller.py
src/aram_nn/lcu/snowball.py
src/aram_nn/lcu/writer_protocol.py
src/aram_nn/lcu/writer_service.py
src/aram_nn/lcu/writer_supervisor.py
src/aram_nn/lcu/writer_transport.py
scripts/lcu_collector.py
scripts/mayhem_lcu_watchdog.py
OPERATIONS.md
runbooks/README.md
runbooks/single-writer-cutover.md
```

`scripts/watchdog_keepalive.ps1` 不必換：它只負責拉起 watchdog，`--workers 2` 語意在新 watchdog 裡變成 fleet producer 數。

## 0a. Divergence check (crawler still running)

**Overlay 是單向覆寫，不是 merge。** Primary 可能在某個檔案上比 branch 新 —— 2026-08-27
就發生過：Classic producer floor 當天 11:32 已經上線、backfill flag 也寫進 DB 了，但
merge branch 早於它、不帶這段程式碼，overlay 直接把已上線的功能覆蓋掉。當時是因為
併發 session 留了測試才被抓到；沒有測試就會靜默回退。

覆寫前先比對，任何「primary 有、branch 沒有」的符號都要先確認是刻意丟棄：

```powershell
$src = "D:\Projects\CODINGram-winrate-nn-single-db-writer-merge-20260827"
$dst = "D:\Projects\CODINGram-winrate-nn"
foreach ($rel in @("srcram_nn\lcu\snowball.py","srcram_nn\lcu\poller.py",
                   "srcram_nn\lcu\db_state.py","scripts\lcu_collector.py",
                   "scripts\mayhem_lcu_watchdog.py")) {
  $d = Compare-Object (Get-Content (Join-Path $dst $rel)) (Get-Content (Join-Path $src $rel))
  $onlyPrimary = @($d | Where-Object { $_.SideIndicator -eq "<=" }).Count
  "{0,-40} primary-only lines: {1}" -f $rel, $onlyPrimary
}
```

`primary-only lines` 不為 0 不代表一定有問題（branch 重構本來就會改寫），但每一個都要
看過。最可靠的訊號是**測試**：先跑一次 primary 的 `python -m pytest tests/ -q` 並記下
結果，overlay 之後再跑一次；overlay 後才開始失敗的測試，就是被覆蓋掉的功能。

## 0. Baseline (crawler still running)

在 primary checkout：

```powershell
$root = "D:\Projects\CODING\aram-winrate-nn"
Set-Location $root
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = "D:\Projects\CODING\aram-winrate-nn-backups\single-writer-cutover-$stamp"
New-Item -ItemType Directory -Force $backup | Out-Null
# Persist the path. Steps 3 and Rollback both need it, and a cutover spans long
# enough that the shell holding this variable may not be the shell that runs
# them. If $backup is silently $null there, the copy-back has nothing to
# restore and the overlay still overwrites -- the one failure this runbook
# cannot absorb.
$backup | Set-Content "$root\data\monitor\last_cutover_backup.txt" -Encoding utf8

python scripts/lcu_collector.py status | Tee-Object "$backup\status-before.txt"
python scripts/lcu_collector.py metrics | Tee-Object "$backup\metrics-before.txt"
Get-Content data/monitor/crawl_metrics.jsonl -Tail 5 | Tee-Object "$backup\metrics-tail-before.txt"

Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'mayhem_lcu_watchdog|lcu_collector.py' } |
  Select-Object ProcessId,Name,CommandLine |
  Format-List |
  Tee-Object "$backup\processes-before.txt"
```

記下 Mayhem／current-patch 場數與兩個 `snowball` PID。Rollback 靠這個 backup 目錄，不是靠 git。

## 1. Freeze keepalive

Keepalive 每分鐘會把死掉的 watchdog 拉起來。換檔前必須停：

```powershell
Disable-ScheduledTask -TaskName MayhemLCUWatchdogKeepalive
Get-ScheduledTask -TaskName MayhemLCUWatchdogKeepalive | Select-Object TaskName,State
```

`State` 必須是 `Disabled`。

## 2. Stop the old fleet

舊 worker 沒有 `--control-file`。用現行 watchdog 的 stop（它認 `snowball` 子命令），再確認 process 清乾淨：

```powershell
python -c "from scripts.mayhem_lcu_watchdog import stop_snowball_workers; print(stop_snowball_workers())"
```

若 import 失敗，直接對 `lcu_collector.py snowball` PID `Stop-Process`。然後停 watchdog 本身：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'mayhem_lcu_watchdog.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

確認沒有人再寫 DB：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'lcu_collector.py|mayhem_lcu_watchdog.py' } |
  Select-Object ProcessId,CommandLine
```

輸出必須是空的。`publish_static_site` / `refresh_recommender_models` 可以留；它們不該長時間持有 write lock。若 overlay 當下 publisher 正在 commit `games.db`，等它結束。

## 3. Backup then overlay

```powershell
$src = "D:\Projects\CODING\aram-winrate-nn-single-db-writer-merge-20260827"
$dst = "D:\Projects\CODING\aram-winrate-nn"
# Re-read rather than trusting a variable from step 0.
$backup = (Get-Content "$dst\data\monitor\last_cutover_backup.txt" -Raw).Trim()
if (-not (Test-Path $backup)) { throw "backup dir missing: $backup" }
$files = @(
  "src\aram_nn\lcu\db_state.py",
  "src\aram_nn\lcu\poller.py",
  "src\aram_nn\lcu\snowball.py",
  "src\aram_nn\lcu\writer_protocol.py",
  "src\aram_nn\lcu\writer_service.py",
  "src\aram_nn\lcu\writer_supervisor.py",
  "src\aram_nn\lcu\writer_transport.py",
  "scripts\lcu_collector.py",
  "scripts\mayhem_lcu_watchdog.py",
  "OPERATIONS.md",
  "runbooks\README.md",
  "runbooks\single-writer-cutover.md"
)
foreach ($rel in $files) {
  $from = Join-Path $src $rel
  $to = Join-Path $dst $rel
  $save = Join-Path $backup $rel
  New-Item -ItemType Directory -Force (Split-Path $save) | Out-Null
  if (Test-Path $to) {
    Copy-Item $to $save -Force
    if (-not (Test-Path $save)) { throw "backup failed for $rel; abort before overwrite" }
  }
  New-Item -ItemType Directory -Force (Split-Path $to) | Out-Null
  Copy-Item $from $to -Force
}

python -c "from aram_nn.lcu.writer_service import WriterService; from aram_nn.lcu.writer_supervisor import WriterSupervisor; print('import-ok')"
```

不要從 worktree 啟動 watchdog：它的 `ROOT` 會變成 worktree，`--db` 預設會指錯地方。

## 4. Start the new fleet

```powershell
Set-Location $dst
$pythonw = Join-Path (Split-Path (Get-Command python).Source -Parent) "pythonw.exe"
$pythonExe = if (Test-Path $pythonw) { $pythonw } else { "python" }
Start-Process -FilePath $pythonExe -ArgumentList @(
  "scripts/mayhem_lcu_watchdog.py",
  "--workers", "2",
  "--degraded-workers", "1",
  "--degrade-client-mb", "5200",
  "--client-restart-mb", "5800",
  "--worker-start-max-client-mb", "6500",
  "--manual-seed-pending-cap", "120",
  "--check-interval-sec", "60",
  "--client-ready-timeout-sec", "600",
  "--games-per-player", "0",
  "--classic-claim-percent", "10",
  "--classic-revisit-min-hours", "10",
  "--classic-revisit-max-hours", "168",
  "--seed-riot-id-file", (Join-Path $dst "data\seeds\opgg_tw.txt"),
  "--static-publish-growth-ratio", "0.10",
  "--static-publish-max-age-hours", "12",
  "--static-publish-threshold", "0",
  "--static-publish-patch-prefix", "auto",
  "--static-publish-auto-patch-min-games", "10000"
) -WorkingDirectory $dst -WindowStyle Hidden
```

Keepalive 留在 Disabled，直到 step 5 驗收通過。開太早的話，新 watchdog 若是
crash-loop，keepalive 每分鐘把它拉回來會讓症狀看起來像「偶爾斷一下」，正好蓋掉
你要判斷的訊號。

## 5. Accept / reject (first 10 minutes)

Process 拓撲必須是：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'lcu_collector.py|mayhem_lcu_watchdog' } |
  Select-Object ProcessId,CommandLine | Format-List
```

必須看到：

- 一個 `mayhem_lcu_watchdog.py`
- 一個 `lcu_collector.py snowball-workers`（帶 `--control-file`）
- **零個** 單獨的 `lcu_collector.py snowball`（沒有 `-workers`）

Log：

| 範圍 | 路徑 | 不該出現 |
|---|---|---|
| Fleet stdout | `.codex/logs/mayhem_lcu_watchdog/snowball_fleet_*.out.log` | `TypeError`、`unexpected keyword` |
| Producer | `.codex/logs/mayhem_lcu_watchdog/snowball_W01.log`、`snowball_W02.log` | `database is locked`、`CLAIMS_STOPPED` 循環、空的立即退出 |
| Producer stderr | `.codex/logs/mayhem_lcu_watchdog/snowball_W0*.err` | traceback |
| Watchdog JSONL | `data/monitor/mayhem_lcu_watchdog.jsonl` | 連續 `start_fleet` 失敗 |

Fleet stdout 應有 `[fleet] supervisor_pid=... writer_pid=... producers=2`。Producer 應開始 `player N/50000` 與 `[saved] Mayhem`。

```powershell
python scripts/lcu_collector.py status
Get-Content data/monitor/mayhem_lcu_watchdog.jsonl -Tail 8
Get-Content .codex/logs/mayhem_lcu_watchdog/snowball_W01.log -Tail 20
```

10 分鐘內 Mayhem／current-patch 場數必須增加。`classic_lambda > 0` 列數不應掉回 0。

失敗立刻走 Rollback，不要「再等一個小時看會不會自己好」。

驗收通過後才恢復 keepalive：

```powershell
Enable-ScheduledTask -TaskName MayhemLCUWatchdogKeepalive
Get-ScheduledTask -TaskName MayhemLCUWatchdogKeepalive | Select-Object TaskName,State
```

## Forbidden while new fleet is live

- `python scripts/lcu_collector.py collect`（自己再起一個 WriterSupervisor）
- `python scripts/lcu_collector.py snowball`（舊直連路徑；watchdog 也看不見它）
- 從 worktree 再起一組 watchdog／fleet
- 移動 `games.db`／WAL／SHM

## Rollback

Keepalive 先 Disable。停新 fleet 用新 watchdog 自己的 stop —— 它會從 cmdline 解出
control file（`data/lcu/.games.db.snowball.stop`）並寫入 `stop`，比手寫路徑可靠：

```powershell
python -c "from scripts.mayhem_lcu_watchdog import stop_snowball_workers; print(stop_snowball_workers())"
```

再 `Stop-Process` 掉 watchdog 本身。確認 process 空了，才把 backup 裡的檔案 copy 回
primary：

```powershell
$dst = "D:\Projects\CODINGram-winrate-nn"
$backup = (Get-Content "$dst\data\monitor\last_cutover_backup.txt" -Raw).Trim()
Get-ChildItem $backup -Recurse -File -Include *.py,*.md |
  ForEach-Object {
    $rel = $_.FullName.Substring($backup.Length + 1)
    Copy-Item $_.FullName (Join-Path $dst $rel) -Force
  }
```

Copy-back **不會**刪掉新增的 `writer_protocol.py` / `writer_service.py` /
`writer_supervisor.py` / `writer_transport.py` —— backup 裡沒有它們的舊版，因為本來
就不存在。留著無害：還原後的 `snowball.py` 不 import 它們。不要為了「乾淨」去刪。

Enable keepalive；舊 watchdog 會把兩個直連 `snowball` 拉起來。

用 baseline 的 status／metrics 確認 Mayhem 再次成長。新 fleet 的 log 留著，不要刪。

## After a good window

連續兩個 Discord 6h 狀態（或約 12 小時 metrics）沒有 fleet 崩潰、Mayhem 成長不差於 cutover 前，才把 Git 整合當成下一步。那是 `runbooks/git-workflow.md` 的事：先處理 primary dirty WIP，再把 `codex/single-db-writer-merge-20260827` 合進可辨識的 main。Agent 不在 cutover 當下自行 merge／push `main`。
