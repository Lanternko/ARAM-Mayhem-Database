# OPGG Seed Refresh Runbook

## Purpose

TW Mayhem 的預設 root seed strategy 是 OPGG/manual Riot ID。這份 runbook 只負責前進 page window、產生可 hydration 的 seed 檔並證明 refresh 成功；長時間 worker lifecycle 仍由 `crawler-stall.md` 的 watchdog 擁有。

## Before refresh

先記錄：

```powershell
python scripts/lcu_collector.py status
python scripts/lcu_collector.py metrics
python scripts/lcu_collector.py family-stats --queue 2400
```

只有在 LCU ready 且證據指向 seed exhaustion 時才 refresh。若是 auth、client memory 或 DB 問題，先走 crawler stall recovery。

## Refresh page window

小範圍驗證可先產生少量 pages：

```powershell
python scripts/lcu_collector.py seed-opgg-plan --region tw --tier platinum --tier gold --pages-per-tier 2 --out data/seeds/opgg_tw.txt
```

正式 refresh 使用可續跑的 state/history，並前進既有 window：

```powershell
python scripts/lcu_collector.py seed-opgg-plan --region tw --tier diamond --tier emerald --tier platinum --tier gold --pages-per-tier 80 --topn-total 0 --resume --out data/seeds/opgg_tw.txt
```

實際 flag 以當前 `python scripts/lcu_collector.py seed-opgg-plan --help` 為準。

## Hydration check

- `data/seeds/opgg_tw.txt` 應包含可解析的 `Name#TAG` 或 OPGG profile URL。
- `data/seeds/opgg_tw_state.json` 與 `data/seeds/opgg_tw_history.jsonl` 都必須前進，才算 page window refresh 成功。
- `manual_riot_id seed progress` 不能長期停在 `resolved=0 / enqueued=0`；反覆為零表示目前 window 已耗盡或解析規則失效。
- 先用小批 seeds 驗證 Riot ID → local PUUID bridge 與 enqueue，再放大 page range。

## Resume crawling

Production 環境讓既有 watchdog 讀取／補充 frontier，不另開第二組 worker fleet。若是人工單次驗證，可用 `snowball` 的小 target probe；確認後停止 probe，回到 watchdog owner。

不要用 `apex`、`ladder` 或 `riot_tier` 填補目前 TW Mayhem frontier；這些 family 已有大量零 transitive capture 的量測證據。

## Success criteria

- State 與 history 的 page cursor 前進。
- 新 seeds 可 resolve、enqueue，且 root family attribution 是 `manual_riot_id`。
- 接續 metrics window 出現 queue/capture/current-patch growth。
- 若仍大量 `target_games=0`，再次前進 OPGG window或檢查 OPGG parser，不要重跑同一批 seeds。
