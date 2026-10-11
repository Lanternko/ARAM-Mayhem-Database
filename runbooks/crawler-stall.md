# Crawler Stall and LCU Recovery Runbook

## Signal

Crawler process 存在不等於健康。`Mayhem +0`、`current_patch +0`，同時 `done_delta` 持續增加，代表 worker 活著但 frontier 或 seed family 低產；若連 LCU request 都失敗，則是 client/auth 狀態問題。

## Diagnose first

先定位 live harness checkout 與資料 owner（見 [OPERATIONS.md](../OPERATIONS.md)）；task worktree 的 source 可用來修改文件，但不能把缺少 ignored log／DB 當作 production 沒運行。

### 告警與自癒時間線

收到歷史告警或「為何沒有自動修復」時，先以同一時區串起告警前後的證據，再決定是否需要 recovery：

- 指定的 `data/monitor/stall_forensics/stall_*.json`、`logs/crawler-stall-alert.log` 與 `data/monitor/stall_alert_state.json`：分別是當時採證、告警／恢復發送紀錄與目前 debounce 狀態。最新 state 不能取代歷史證據。
- `logs/mayhem-lcu-watchdog-keepalive.log` 與 `data/monitor/mayhem_lcu_watchdog.jsonl`：比對 parent 心跳、LCU health、worker 數、資源狀態與 recovery actions。先依事件時間篩選、摘要必要欄位，避免輸出整段 command line／credentials／玩家資料。
- 告警的 `capture_age_min` 是距上次收場的時間，包含主機／collector 沒運行的空窗；`down_for_min` 是本輪首次偵測到告警以來的時間。兩者不能當作 watchdog 持續失敗的時間；首次告警顯示 0 分鐘不代表剛停止收場。精確語義以 live `scripts/crawler_status_discord.py` 為準。
- 告警發出後仍需查後續事件：watchdog 可能正在啟動 League、等待 LCU ready 或恢復 workers；若已恢復且持續成長，不再觸發第二次重啟。

若 keepalive／watchdog 同時有長時間空窗，先查 Windows 與排程，而不是歸因 seed 或 LCU：

```powershell
$stallTask = Get-ScheduledTask -TaskName MayhemLCUWatchdogKeepalive
$stallTask.Principal | Format-List LogonType,RunLevel
$stallTask.Settings | Format-List StartWhenAvailable,WakeToRun,DisallowStartIfOnBatteries,StopIfGoingOnBatteries
$stallTask.Triggers | Format-List *
Get-ScheduledTaskInfo -TaskName MayhemLCUWatchdogKeepalive | Format-List LastRunTime,LastTaskResult,NextRunTime
```

依事件時間窗查 System log 的 Kernel-General 開關機、Kernel-Power／Power-Troubleshooter 睡眠與恢復，以及 TaskScheduler Operational（若已啟用）的執行結果。`Interactive`／`InteractiveToken` 需要已登入 session；每分鐘 trigger 不代表登入前也會執行。Log 空窗只證明缺少紀錄，不能單獨證明整段未登入、睡眠或斷電；Kernel-Power 41 也不能單獨判定是哪種非正常關機原因。確認事實、推論與未知部分分開回報，不因這個發現就改成 SYSTEM 或自動登入。

### 仍在停收時

從 live repo root 記錄同一時間點的證據；若只是解釋已恢復的歷史告警，可使用既有同時段 metrics，避免重跑昂貴統計：

```powershell
python scripts/lcu_collector.py status
python scripts/lcu_collector.py metrics
python scripts/lcu_collector.py family-stats --queue 2400
```

再檢查：

- Worker/watchdog command line、最近 stdout/stderr 與 `target_games` 分布。
- League/LCU 是否 ready，credentials 是否因 client restart 換了 port/token。
- Queue 是否仍有 pending items、`crawl_seen` 是否快速增加、capture 是否成長。
- 最近 seed family 的 per-family ROI；不要只看 immediate `source=match` attribution。

## Interpret

- 確認 parent 未運行／排程未執行：先處理 harness availability，LCU recovery 沒有執行者；只有心跳缺失時仍需交叉驗證 process 與排程，不能直接歸因 client 修復條件失效。
- 資源保護暫停或恢復樣本尚未達標：比對 action 當時的 resource state、memory 與實際 argv。暫停不是 seed exhaustion，也不能用累積停收時間繞過 phase 保護；缺少讀值不當作資源充足。唯一例外是 client 自己就是壓力來源：不安全 phase、沒有 `League of Legends.exe`、client 高於 worker 啟動上限且連續達到 `--unsafe-phase-idle-restart-after-min`，watchdog 會重啟 client；這條不看停收時間。
- 已有 recovery action：分辨等待 ready、重試失敗、workers 已拉起但未收場，以及已持續成長。Action 名稱或 process 存在本身不是成功證據。
- LCU 401/connection failure：重新抓 current credentials 與 `current_summoner`；通常是 restart 後 port/token 變更或 `/lol-*` 尚未 ready，不是 TLS cert 真過期。
- Riot remoting 回 424：現有 Riot Client 的 product launcher 無法開 League；watchdog 必須殺掉 Riot Client 再冷啟動。不要對同一個 instance 反覆 POST。
- Workers alive、done 增加、幾乎全是 `target_games=0`：active subgraph 已吃乾，換 seed page window。
- `recent-active` 只短暫打開 queue，隨即回到零產出：換 root seed family，不要反覆 recent-active。
- `manual_riot_id`/OPGG 是已驗證 productive family；舊的 manual yield=0 是 attribution bug 結論，不可沿用。
- `apex`、`ladder`、`riot_tier` 在目前 TW Mayhem 已是 dead family；除非換 region 或大版本後重新量測，否則不要消耗 LCU bandwidth。
- Suggested players 只有 lobby phase 才可能存在；phase=None 時不應把零結果當 bug。

## Recovery order

1. 先保留與原因相關的時間線、status、metrics／family stats 與 log 片段；已恢復就停止 recovery 分支。
2. 先確認 watchdog 有運行；若是資源保護，依 production 恢復條件觀察。若是 auth/LCU 狀態，讓既有 watchdog 完成 client ready 與 credentials 刷新；依目前重試／等待設定判斷進度，不另開競爭的重啟流程。
3. 若是 seed exhaustion，依 `opgg-seed-refresh.md` 前進 OPGG page window。
4. 讓 watchdog 管理 worker count；不要同時手動再開另一組 workers。
5. 觀察新的 seeds 是否讓 queue、capture 與 current-patch games 恢復成長。

## Production watchdog

不在本 runbook 複製 production 門檻。設定 owner 與說明見 [OPERATIONS.md](../OPERATIONS.md)，啟動參數來源是 live `scripts/watchdog_keepalive.ps1`；目前 process argv 與 action 當時的 recovery JSONL 才能證明實際套用值。三者不一致時記錄差異，不用 Python generic defaults 或舊文件推定 runtime 行為。

Watchdog recovery JSONL 應保留 action、thresholds、LCU status／phase，以及當時 client memory 與可用的 system resource state。調參或恢復前核對這些證據；不能把資源保護造成的停收視為 client 卡住而觸發以停收時間為準的 phase 例外；沒有對局程序且 client 過大的 idle 例外不依賴停收時間，recovery reason 會寫明 `no game process`。

## League restart guardrail

不要直接啟動 `LeagueClient.exe`，Riot 會回 `Access is denied`。正確路徑是取得 Riot Client Electron 的 `--app-port` 與 `--remoting-auth-token`，再以 basic auth `riot:<token>` 對 product launcher endpoint 發出 League launch request。優先讓 watchdog 實作這段流程。

## Success criteria

- LCU health/request 恢復成功。
- Queue 不再只消耗零產出節點。
- Mayhem capture 與 current patch count 在連續 metrics window 成長，並明示 queue、patch、region、時間窗與資料來源。全 queue capture watermark 變新或 Discord 宣告恢復，只能證明其對應口徑；沒有 scoped metrics 時明說 Mayhem／current-patch 成長尚未驗證。
- 沒有重複 worker fleet、DB lock storm 或跨 client 共寫同一 DB。
