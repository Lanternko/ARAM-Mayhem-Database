# ARAM Mayhem Collector / Winrate NN

這個 repo 已經從早期的「單純 LCU collector 工具」升級成完整的：

- `Mayhem / ARAM` 本機 LCU 收集器
- `priority snowball crawler`
- `OPGG seed` 自動擴散
- `SQLite -> parquet` 匯出流程
- `NN / LR` 訓練與評估 pipeline
- `auto_train` 與 crawl metrics 監控

目前主線內容以 `D:/Projects/CODING/aram-winrate-nn` 為基礎，這個 repo 保留既有 GitHub 歷史與發佈位置。

## 你要用哪一種模式？

### 1. 只想收集資料

最簡單的入口是：

```powershell
python collect.py
```

它會：

1. 使用目前調過的 `snowball-workers` 參數啟動 crawler
2. 等待 crawler 把 frontier 跑完
3. 匯出 `my_games.parquet`

如果你想直接用底層 CLI：

```powershell
python lcu_collector.py status
python lcu_collector.py snowball-workers --workers 4 --target-games 50000 --max-players 50000 --games-per-player 4 --manual-seed-pending-cap 40
python lcu_collector.py export --queue 2400 --out data/raw/mayhem_games.parquet
```

### 2. 想跑研究 / 訓練

安裝 editable package：

```powershell
python -m pip install -e .
```

訓練與評估入口：

```powershell
python -m aram_nn.train --data data/raw/mayhem_games.parquet --out models/run1
python -m aram_nn.eval --help
python auto_train.py
```

## 目前 crawler 主策略

- `match` 來源高 priority
- `manual_riot_id` 先做 `recent history` 預篩選，沒有 `2400/450` 就不 enqueue
- `--manual-seed-pending-cap 40`
- `OPGG later-page resume cursor`
- `metrics` 追蹤每小時成長、目前 patch 成長、frontier 效率

## 重要路徑

- 主 DB：`data/lcu/games.db`
- OPGG seed state：`data/seeds/opgg_tw_state.json`
- OPGG seed history：`data/seeds/opgg_tw_history.jsonl`
- Crawl metrics：`data/monitor/crawl_metrics.jsonl`
- 主 CLI：`scripts/lcu_collector.py`

## 常用指令

```powershell
python scripts/lcu_collector.py status
python scripts/lcu_collector.py metrics --record
python scripts/lcu_collector.py seed-opgg-plan --resume --start-page 81 --state-file data\seeds\opgg_tw_state.json --history-file data\seeds\opgg_tw_history.jsonl --region tw --tier diamond --tier emerald --tier platinum --tier gold --pages-per-tier 1 --topn-total 200 --out data\seeds\opgg_tw.txt
python scripts/lcu_collector.py snowball-workers --workers 4 --seed-riot-id-file data\seeds\opgg_tw.txt --manual-seed-pending-cap 40 --target-games 50000 --max-players 50000 --games-per-player 4
```

## Repo 說明

- `src/aram_nn/lcu/`：LCU / snowball crawler
- `src/aram_nn/ingest/`：Riot API snowball / extract
- `src/aram_nn/models/`：LR / DeepSets
- `scripts/`：collector、probe、backfill、smoke test
- `auto_train.py`：定期檢查 DB 增量並自動訓練

## 注意

- `data/`、`logs/`、`models/` 預設都不應提交
- Mayhem `queueId=2400` 無法靠公開 Riot API 直接抓整場，只能走本機 LCU
- `auto_train.py` 現在預設讀的是本 repo 的 `data/lcu/games.db`
