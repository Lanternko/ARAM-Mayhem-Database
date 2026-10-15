# Crawler 降速診斷與修正（2026-10-15）

## 決策與範圍

修正單一 SQLite writer 被首次拜訪查詢長時間占住，以及單次 LCU 探測失敗立即重啟的行為。保留 worker 數、記憶體保護、首次拜訪比例、Classic 比例與原本排序，不以增加請求量解決停收。

資料來源為本機 private `data/lcu/games.db`、watchdog JSONL 與 fleet logs；region TW，Mayhem queue 2400，patch `16.20.824`。以下速率按 `captured_at`，不是對局開始時間，且為整段 wall-clock 平均，包含中斷。

| 台灣時間 | 新增 Mayhem | 場／分 |
|---|---:|---:|
| 10/11 全天 | 59,629 | 41.4 |
| 10/13 全天 | 54,739 | 38.0 |
| 10/14 全天 | 38,563 | 26.8 |
| 10/15 00:00–約 18:29 | 27,188 | 24.5 |

## 證據與機制

- 原首次拜訪查詢使用一般 pending index，按 recency 走 frontier，對每筆玩家再查 `crawl_seen.process_count > 0`。同時仍有約 12.9 萬筆未拜訪玩家，但排在前面的 revisit backlog 讓查詢遲遲無法返回第一筆。
- 18:34 左右的 live read-only probe 設定 5 秒 progress-handler 上限；首次拜訪查詢被中斷，一般 claim 約 0.3 ms，pending count 約 55 ms，Classic due 約 12 ms。查詢耗時會阻塞 writer 的另一個 producer，排隊後可能超過 30 秒 RPC deadline。
- 10/15 12:00–12:59 只新增 4 場。12:14 UTC+8 啟動的 fleet 兩個 producer 各失敗六次而退役，最後整組退出。累積 producer stderr 有 `WRITER_RESPONSE_TIMEOUT`、`MISMATCHED_WRITER_RESPONSE`；後者可由上一個逾時請求的遲到回覆引發。
- 下午有多次 LCU health failure；現場同一個 localhost endpoint 三次依序約 4.13 s、12 ms、12 ms，證明延遲會大幅波動，但不能據此斷言每次歷史失敗都是假警報。既有 log 只有 body byte count，無法還原每次錯誤種類。
- `423 Locked` 是部分 client 啟動請求的回應；同輪後續 LCU 仍可能 ready，不能單憑該 status 認定完全沒有啟動成功。

## 實作

1. 在 `crawl_queue` 維護 `has_been_crawled`。一次性由 canonical `crawl_seen.process_count > 0` 回填，再用 triggers 維護 seen insert／process_count update 與 queue insert，涵蓋 legacy 與 RPC writer、defer 與重建 queue row。
2. 首次拜訪 partial index 完整沿用原本 ORDER BY，排除已拜訪玩家；仍保留 canonical `NOT EXISTS` guard。新欄位不改對局資料，也不改推薦統計。
3. 不能用 `classic_last_crawl_ms = 0` 代替 canonical 判斷：本次 snapshot 有 272,403 筆 rank-0 pending 玩家已拜訪但 Classic timestamp 為零，第一版篩選仍需約 2.8 秒，故未採用。
4. Watchdog 對失敗健康探測隔 1 秒再查一次；持續失敗仍在同一 tick 進原恢復分支。新增每次 probe 的耗時、狀態與錯誤類型，不記錄玩家回應內容。

## 驗證

從 live frontier 以單一 SELECT 讀取 767,855 筆 queue 與對應 process_count，寫入獨立 SQLite fixture。玩家識別改成 `fixture-<queue_idx>`；不複製 games、PUUID 或 Riot ID。同一份 fixture、相同 now_ms 對照原查詢與修改後查詢：

| 指標 | 結果 |
|---|---:|
| 原查詢 | 6.107 s |
| 新查詢第一次 | 0.185 ms |
| 新查詢後兩次 | 0.021／0.018 ms |
| 挑選結果 | 相同 |
| 一次性 migration＋index 建立 | 2.064 s |
| 再次 schema check | 0.173 ms |

這是 sanitized fixture 上的 SQL 成本，不能直接當作 production 收場倍率。Local artifact：`outputs/crawler-throughput/{frontier-sanitized.db,benchmark-v2.json}`。

執行所有 `test_lcu*`、resource guard 與 watchdog 測試：147 passed。涵蓋 bounded SQLite VM work、legacy／RPC 挑選結果、migration、trigger 的兩種寫入順序與 rollback、健康探測瞬間恢復及持續失敗。`git diff --check` 通過。

## 套用與回復

Source 必須先整合到 live checkout，然後讓既有 watchdog parent 與 fleet 重新載入；publisher、model refresher 不需重啟。第一次 writer 啟動會新增欄位、triggers、partial index 與 migration watermark。不得在另一個程序對 live DB 同時手動跑 migration。

回退 source 不需要刪欄位、index 或 watermark；它們為 additive metadata，舊 source 可繼續運作。若要回復到舊 schema，需另外停 collector 並按維運流程處理，不能在 live DB 拆欄位或移動 DB／WAL／SHM。

套用後驗收必須核對新 process creation time、`lcu.probes`、新 index／migration watermark、連續 queue-2400 current-patch 成長及新 fleet error log。短窗速率只能證明當下狀態，不能宣稱已恢復長期 45 場／分。
