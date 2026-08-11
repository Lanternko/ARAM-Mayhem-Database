# OPERATIONS — 本機 harness 總圖

這是「這台 Windows 主機有哪些常駐鏈、誰負責拉起、設定在哪裡、怎麼觀測與重建」的單一入口。Crawler stall 與網站 deploy 的精確判斷流程留在 `runbooks/`，不在這裡複製。

## 1. 拓撲

```text
MayhemLCUWatchdogKeepalive（Task Scheduler，每分鐘）
└─ scripts/watchdog_keepalive_hidden.vbs
   └─ scripts/watchdog_keepalive.ps1
      └─ pythonw scripts/mayhem_lcu_watchdog.py
         ├─ lcu_collector.py snowball：W01、W02 共用 games.db frontier
         ├─ publish_static_site.py --watch：資料成長門檻發布 GitHub Pages
         └─ refresh_recommender_models.py --watch：成長門檻更新本機推薦模型

ARAMMeta API／Tunnel（Task Scheduler，登入時啟動）
├─ data/site/run_meta_pick_api.py
├─ data/site/run_arammeta_tunnel.py
└─ ARAMMeta Watchdog（登入時＋每分鐘）自癒 API 與 tunnel
   └─ data/site/watch_arammeta.py

ARAMMeta Backup（Task Scheduler，每日 03:15）
└─ data/site/backup_meta_pick_db.py

Crawler 通知（Task Scheduler）
├─ ArammetaCrawlerStallAlert：每 5 分鐘，只有異常／恢復才送 Discord
└─ ArammetaCrawlerStatusDiscord：每 6 小時送狀態摘要
```

本機 GUI「ARAM Recommender (source)」直接啟動 `pythonw scripts/recommend_gui.py`，不屬於上述常駐鏈；舊 exe build 路徑已棄用。

## 2. 設定所有權

| 設定 | Source of truth | 現行 production profile |
|---|---|---|
| 排程入口 | `notes/task-definitions/MayhemLCUWatchdogKeepalive.xml` | 每分鐘呼叫 hidden VBS |
| Crawler／publisher／model wrapper | `scripts/watchdog_keepalive.ps1` | 2 workers；degraded 1；adaptive `games-per-player=0` |
| League memory | 同上 | degrade 5200 MB；safe restart 5800 MB；worker start gate 6500 MB |
| Frontier | 同上＋watchdog | manual pending cap 120；queue 450／2400／2450／4310；OPGG＋self＋friends |
| Static publish | wrapper＋`src/aram_nn/site/static_publish.py` | growth 10%；patch `auto`；新 patch 至少 10,000 games |
| Recommender refresh | `scripts/mayhem_lcu_watchdog.py` defaults | growth 25%；目前 patch 至少 15,000 games |
| 生成站台 allowlist | `src/aram_nn/site/static_publish.py::DEFAULT_DOC_PATHS` | atomic build／stage／commit／push |
| ARAMMeta API tasks | `data/site/install_arammeta_tasks.ps1`（本機、gitignored） | API、Tunnel、每分鐘 Watchdog、03:15 Backup |

`mayhem_lcu_watchdog.py --help` 顯示的是通用 CLI defaults；這台機器以 `watchdog_keepalive.ps1` 的參數覆寫為準。調整 production profile 時改 wrapper，不要只改 Python default 或備份 XML。

## 3. 狀態與 log

| 範圍 | 位置 |
|---|---|
| Keepalive 啟動／心跳 | `logs/mayhem-lcu-watchdog-keepalive.log` |
| Watchdog stdout／stderr | `logs/mayhem-lcu-watchdog.out.log`、`.err.log` |
| Watchdog recovery events | `data/monitor/mayhem_lcu_watchdog.jsonl` |
| Worker stdout／stderr | `.codex/logs/mayhem_lcu_watchdog/snowball_W*.{out,err}.log` |
| Crawler growth metrics | `data/monitor/crawl_metrics.jsonl` |
| Static publisher | `data/site/static_publish.{out,err}.log`、`static_publish_state.json` |
| Model refresh | `data/site/model_refresh.{out,err}.log`、`model_refresh_state.json` |
| Discord crawler tasks | `logs/crawler-stall-alert.log`、`logs/crawler-status-discord.log` |
| ARAMMeta API／tunnel／backup | `data/site/` 下對應 `.log`、`.out.log`、`.err.log` 與 `backups/` |

State JSON／JSONL 是恢復與門檻判斷依據，不是可隨手清除的 cache。`games.db`、`games.db-wal`、`games.db-shm` 在 collector 執行時絕對不要移動、替換或分開處理。

## 4. 快速觀測

```powershell
# 排程狀態
Get-ScheduledTask | Where-Object { $_.TaskName -match 'Mayhem|ARAMMeta|ArammetaCrawler' } | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName MayhemLCUWatchdogKeepalive

# Watchdog、兩個 worker、publisher、model refresher
Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'mayhem_lcu_watchdog|lcu_collector.py snowball|publish_static_site|refresh_recommender_models' } | Select-Object ProcessId,Name,CommandLine | Format-List

# Collector 與最近 harness event
python scripts/lcu_collector.py status
Get-Content data/monitor/mayhem_lcu_watchdog.jsonl -Tail 10
Get-Content logs/mayhem-lcu-watchdog-keepalive.log -Tail 10

# Publisher／model state
Get-Content data/site/static_publish_state.json
Get-Content data/site/model_refresh_state.json
```

不要只用「process 存在」判斷 crawler 健康；workers alive 但 Mayhem 不成長時，依 `runbooks/crawler-stall.md` 先記錄 metrics、查 LCU 與 seed yield。

## 5. Lifecycle 與故障分流

- Keepalive 每分鐘只檢查 watchdog parent；parent 消失會被拉起，parent 再確保 workers、publisher、model refresher 存活。調參後需讓舊 parent 結束並由排程重生，否則仍跑舊 argv。
- Watchdog 只在安全 gameflow phase 重啟 League，並記錄 action 時的 League memory、門檻與 LCU status。不要直接啟動 `LeagueClient.exe`；重啟必須經 Riot Client launch／remoting 路徑。
- `workers alive + Mayhem 不成長`、大量 `target_games=0`、LCU auth 或 seed family 問題：用 `runbooks/crawler-stall.md`；需要換 OPGG page window 時再用 `runbooks/opgg-seed-refresh.md`。
- Harness 只擁有 routine data publisher：它在 disposable isolated worktree 依成長門檻更新資料，development worktree dirty 不應阻擋或被覆寫。Frontend shell publish 是 task workflow，不是 watchdog child；兩者都只發布 GitHub Pages 靜態檔案，不會重啟 crawler、API、tunnel 或其他 runtime。失敗先看 `data/site/static_publish.err.log`，lane 與操作依 `runbooks/site-deploy.md`；一般 Git／worktree lifecycle 依 `runbooks/git-workflow.md`。
- `sync_site_backend.py --watch` 是本機 DB → 公開 games backend 的獨立鏈，不由 crawler watchdog 啟動；首次灌資料才使用 `--force`。Public schema 會拒絕 PUUID、summoner name、Riot ID 與 UUID-like PUUID。
- OPGG、crawler、publisher 正在跑時，不整理 `data/lcu/`；舊 log 只在 collector 完全停止後移到 `logs/archive/`，SQLite 三件套永遠排除。

## 6. 重建

主 crawler keepalive 可由 tracked XML 重建；目前這台機器上的 ARAMMeta API 四個 tasks 可由本機 installer 重建：

```powershell
schtasks /create /tn MayhemLCUWatchdogKeepalive /xml notes\task-definitions\MayhemLCUWatchdogKeepalive.xml
powershell -NoProfile -ExecutionPolicy Bypass -File data/site/install_arammeta_tasks.ps1
```

`data/site/` 整體被 gitignore，因此 ARAMMeta installer／launchers、`ARAM_SITE_ADMIN_TOKEN` 與 `%USERPROFILE%\.cloudflared\config.yml` 都不會隨 clone 回來。`ArammetaCrawlerStallAlert` 與 `ArammetaCrawlerStatusDiscord` 也只有本機 Task Scheduler 定義，repo 尚無 installer／XML。重灌前必須另做安全備份；不能把「目前機器上存在」當成可重建 harness。

## 7. 相關文件

- Repo 全域不變量：`AGENTS.md`
- 實驗、資料 lineage 與 promotion gate：`notes/agents/experiments-and-data.md`
- 網站設計、CSS token 與 visual QA：`DESIGN.md`
- Crawler stall／LCU recovery：`runbooks/crawler-stall.md`
- OPGG seed refresh：`runbooks/opgg-seed-refresh.md`
- Git／worktree workflow：`runbooks/git-workflow.md`
- Tier-list 發布：`runbooks/site-deploy.md`
- 腳本索引：`scripts/README.md`
- 現行模型、資料與實驗決策：`MODEL.md`
- 歷史模型計畫與快照：`PLAN.md`、`notes/archive/`
