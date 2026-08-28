# Crawler Stall and LCU Recovery Runbook

## Signal

Crawler process 存在不等於健康。`Mayhem +0`、`current_patch +0`，同時 `done_delta` 持續增加，代表 worker 活著但 frontier 或 seed family 低產；若連 LCU request 都失敗，則是 client/auth 狀態問題。

## Diagnose first

從 repo root 記錄同一時間點的證據：

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

- LCU 401/connection failure：重新抓 current credentials 與 `current_summoner`；通常是 restart 後 port/token 變更或 `/lol-*` 尚未 ready，不是 TLS cert 真過期。
- Riot remoting 回 424：現有 Riot Client 的 product launcher 無法開 League；watchdog 必須殺掉 Riot Client 再冷啟動。不要對同一個 instance 反覆 POST。
- Workers alive、done 增加、幾乎全是 `target_games=0`：active subgraph 已吃乾，換 seed page window。
- `recent-active` 只短暫打開 queue，隨即回到零產出：換 root seed family，不要反覆 recent-active。
- `manual_riot_id`/OPGG 是已驗證 productive family；舊的 manual yield=0 是 attribution bug 結論，不可沿用。
- `apex`、`ladder`、`riot_tier` 在目前 TW Mayhem 已是 dead family；除非換 region 或大版本後重新量測，否則不要消耗 LCU bandwidth。
- Suggested players 只有 lobby phase 才可能存在；phase=None 時不應把零結果當 bug。

## Recovery order

1. 先保留 status、metrics、family stats 與相關 log 片段。
2. 若是 auth/LCU 狀態，等待 client ready 並刷新 credentials。
3. 若是 seed exhaustion，依 `opgg-seed-refresh.md` 前進 OPGG page window。
4. 讓 watchdog 管理 worker count；不要同時手動再開另一組 workers。
5. 觀察新的 seeds 是否讓 queue、capture 與 current-patch games 恢復成長。

## Production watchdog

目前 production profile：

```powershell
python scripts/mayhem_lcu_watchdog.py --check-interval-sec 60 --workers 2 --degrade-client-mb 5200 --degraded-workers 1 --client-restart-mb 5800 --worker-start-max-client-mb 3500
```

它在 League client memory 過高時降為 1 worker，並只在安全 phase 重啟 client。Watchdog recovery JSONL 必須記錄 action 當下 `league_main_mb_at_action`、thresholds 與 LCU status，讓後續能用證據調整上限。

## League restart guardrail

不要直接啟動 `LeagueClient.exe`，Riot 會回 `Access is denied`。正確路徑是取得 Riot Client Electron 的 `--app-port` 與 `--remoting-auth-token`，再以 basic auth `riot:<token>` 對 product launcher endpoint 發出 League launch request。優先讓 watchdog 實作這段流程。

## Success criteria

- LCU health/request 恢復成功。
- Queue 不再只消耗零產出節點。
- Mayhem capture 與 current patch count 在連續 metrics window 成長。
- 沒有重複 worker fleet、DB lock storm 或跨 client 共寫同一 DB。
