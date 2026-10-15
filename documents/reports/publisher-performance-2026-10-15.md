# Publisher 效能修正與分階段量測（2026-10-15）

## 決策與範圍

目標是減少不改變統計定義的重複工作，並把本機等待、計算與遠端 Pages 發布分開記錄。
驗證使用 Mayhem queue 2400、patch 16.20、本機 TW LCU 資料；背景 collector 持續運作，
因此兩輪完整演練不是固定資料、固定主機負載的 paired benchmark。
驗收為 task worktree 中的完整 dry-run，沒有 push 或切換 production runtime。
前置 warm-up 在補齊歷史快照後結束，因為它在最後的 metadata 優化完成前就已載入 engine；
其整輪耗時不作為新版成績，僅採用已完成的歷史快照階段數字。

## 原有成本的證據

- 最近五次本機建置為 28.77–86.54 分鐘；其中 tier-list 為 18.75–77.24 分鐘。
  2026-10-15 17:20 的一次為 43.37 分鐘：tier-list 34.94、雷達 5.30、能力軸 2.54、Classic 0.20。
  這些是已進入 heavy-job 後的耗時，不含 admission 等待。
- Git 追蹤的 `docs/` 約 32.39 MiB；本機額外約 168.94 MiB HD 圖大多未追蹤，
  不屬於 publisher 的 atomic allowlist。135.28 GiB 的 private SQLite 也不會上傳。
- 最近兩次 GitHub Pages Actions 為 [95 秒](https://github.com/Lanternko/ARAM-Mayhem-Database/actions/runs/38530695432)
  與 [33 秒](https://github.com/Lanternko/ARAM-Mayhem-Database/actions/runs/38717531426)，與主要本機計算分開。
- 舊雷達單一 child 的 private memory 峰值約 5.8–6.1 GiB；舊 site admission 要求可用 RAM 至少 6 GiB。
  資源記錄中另有 50–60 分鐘 admission 等待，不能誤稱為統計運算。

## 修正

1. 數字 patch prefix 改為等價的 BINARY range，讓既有 `(queue_id, patch, created_ms)` index
   同時限制 queue 與 patch；保留非數字 prefix 的原有 LIKE 行為。原機 1,000 rows 三次測量，
   LIKE 中位數 0.9733 秒、range 0.0344 秒，這個 28.3 倍僅代表樣本 query。
2. 同輪 current-patch 統計共用一次 JSON 解析的暫存 replay，每批 512 場，只保留統計需要欄位。
   多個推薦統計不再反覆讀取幾 GiB 原始 JSON；暫存隨 build 結束清除。
3. 道具風格與增幅分類預先建立查表，不再逐 participant 重做相同 metadata 分類。
   3,000 場 fixture 的分類計算由 4.74 秒降為 0.85 秒，輸出完全相等；這是單一階段的樣本測量。
4. 雷達的 champion LR 使用等價 sparse matrix，保留跨隊 mirror pick 的 red-last assignment。
   LR 的演算法與超參數選擇未變。分位數校準再以連續 double buffer 取代 Python float lists，
   保留原有計分順序與精確 quantile，避免保存約 7,800 萬個 boxed float。
   同一批 score buffers 再供殘差統計使用，不重算第二次隊友分數；`np.add.at` 保留每格的
   原有浮點累加順序，省略本來就沒有輸出到產品的 continuous-residual 中間結果。
5. 雷達與能力軸加入 private dependency cache：pooled parquet 檔案 identity、實際消費英雄欄位、
   選項、Python source 與套件版本一致才沿用，內容有 SHA-256 驗證。
6. growth/check-only 在 resource admission 前執行；實際建置保留共用 lock 與 active stop guard。
7. 每輪保存階段 timing JSON 與 JSONL history，失敗也保存已執行階段；略過更新不覆蓋上次實際 build。

## 正確性驗證

- 95 項相關測試通過，涵蓋 prefix 語義／index plan、read-only loader、snapshot、推薦、重播、
  cache invalidation、sparse LR、quantile 精確等價、資源 guard、publisher 與 timing error handling。
- 同一批 3,000 場真實 queue 2400／16.20 fixture，以 `origin/main` 舊版 engine 對比新版，
  勝率、增幅分類、道具對、單件、鞋子、技能、core-item counters、出裝路線共八組輸出完全相等。
  私有 fixture SHA-256：`6ade1f2ac01f695f8f4a22610f2a8e656493ede3bf17b287fbc7f77a1d8a7d28`。
- sparse 與 dense matrix 逐格相等；測試 fixture 的 LR 機率差在 `1e-7` 內。
- 額外探索 `test_semantic_scores.py` 有既有 Tryndamere／Leona wave score assertion 失敗；
  未修改的 primary checkout 也可重現，與本次效能修改無關。

## 量測解讀

階段 parent 已包含 child，不可把巢狀耗時直接相加。首次全新 worktree 缺少已結束 patch 的
settled counters，其補建成本須獨立看；一般發布沿用既有歷史 counters。
pooled artifact cache 只有依賴完全相同才命中，輸入或能力向量改變仍須重算。

### 完整 dry-run

本輪 current-patch 統計為 329,541 場；獨立隊伍校準稍晚讀取 329,547 場，
背景 collector 持續新增資料，並非同一時間的 DB copy。
pooled 分析為 1,301,458 場、patch 16.18／16.19／16.20，
輸入 `mayhem_pooled_auto.parquet` 為 2,015,561,750 bytes，mtime UTC `2026-10-13T20:24:07.635701+00:00`。

| 互不重複的主要階段 | 秒 | 分:秒 |
|---|---:|---:|
| 資源 admission 等待 | 60.01 | 1:00 |
| tier-list 完整建置 | 899.43 | 14:59 |
| 雷達（cache miss，compact quantile 修正前） | 321.72 | 5:22 |
| 能力軸（cache miss） | 182.62 | 3:03 |
| Classic | 11.48 | 0:11 |
| Git 本機檢查及其他 orchestration | 0.95 | 0:01 |
| **整輪實測** | **1,476.21** | **24:36** |

這是本機完整 dry-run；commit、push、disposable worktree fetch／建立與 GitHub Pages 未執行。
純計算／本機工作扣除 admission 為 23:36，相較最近一次 43.37 分鐘的本機建置，
觀察值少約 45.6%；它不是相同 rows／相同主機負載的 paired speedup。

| tier-list 內部階段（已含於 14:59，不另加總） | 秒 |
|---|---:|
| 原始 JSON 解析／participant projection | 131.70 |
| 當前版本勝率 | 57.59 |
| 版本道具比較（含目前 counters） | 57.51 |
| 完整戰鬥數據／能力向量刷新 | 122.01 |
| 隊伍分數校準 | 42.03 |
| 增幅／道具分類推薦 | 144.71 |
| 道具對 | 44.46 |
| 單件道具 | 68.30 |
| 鞋子 | 54.47 |
| 技能組合 | 25.34 |
| 出裝路線 | 109.62 |
| 兩次 HTML render＋route shells | 4.25 |

Projection 產生 324,367,388 bytes（約 309 MiB）的暫存 replay，完成後已刪除。
剩餘時間為 imports、metadata、artifact 組裝、驗證與其他未細分工作。

### 雷達記憶體修正與快取重測

完整 dry-run 先用 4 GiB admission candidate 量測，發現 sparse LR 之外的 quantile float lists
仍帶來高記憶體峰值，因此沒有將 4 GiB 提升為 production default。
最後改為 compact double buffers，並採 5 GiB admission、2 GiB active stop floor。
此後只重測相同 pooled 輸入與已生成 payload 的雷達／能力軸，不重跑未變更的 tier-list。

| 最終分析階段 | cache miss 秒 | 依賴不變的 cache hit 秒 |
|---|---:|---:|
| 雷達全部 | 150.64 | 0.71 |
| └ 載入／sparse LR | 18.05 | — |
| └ 計分／精確分位數 | 124.55 | — |
| └ 重播分數／殘差累計 | 1.82 | — |
| 能力軸全部 | 166.39 | 0.83 |

殘差階段原本重新計算隊友分數需 163.82 秒，重播後為 1.82 秒；雷達整體約減半。
更改 quantile 儲存與 score replay 前後，完整 1,301,458 場雷達 JSON **逐位元相同**，
SHA-256 為 `17beb1d0a16564c65e1ba89f62eda5db10d76a71df4f415facda01180d478693`。
兩個 cache-hit 產物也與原輸出逐位元相同，計時包含子程序啟動、lookup 與輸出寫入。

| 雷達 sampled child-tree 峰值 | 修改前（sparse 但仍用 float lists） | 最終版 |
|---|---:|---:|
| Private memory | 5.93 GiB | 3.61 GiB |
| RSS | 4.89 GiB | 2.55 GiB |

記憶體來自共用 heavy-job 每秒取樣；以 parent PID **及本輪時間區間** 篩選，避免 Windows PID
重用混入先前工作。Private 約少 39%，RSS 約少 48%；它們是採樣峰值，並非 hard allocation cap。

將上述最後分析時間代入已完成的其他階段，最終版 cold 整輪**合計估值約 21 分 29 秒**；
兩個 pooled artifact 依賴均不變時，合計估值約 **16 分 13 秒**。這兩個是分階段合計，
不是另外兩次 end-to-end 實測。資料、向量、參數或 source 改變時 cache 必須失效，
不承諾日常每輪都能命中；剩餘主要成本是當前版本約 330 萬 participant 的推薦統計與完整戰鬥數據刷新。

原始 stage rows、scope、完整演練、最後分析、cache-hit 與記憶體數字保存在本機
`outputs/site-publish/report-metrics.json`（依 repo 的 generated-artifact 政策不進 Git）；
核心數字與驗證方法保留於本報告。新 CLI 的 timing JSON／JSONL
會繼續保留每輪實際成本；本次修正留在 task branch，尚未 push、切換正式 publisher 或重啟其他服務。

實測 runtime：Python 3.13.1、NumPy 2.4.4、SciPy 1.17.1、scikit-learn 1.8.0、
Polars 1.40.1、PyArrow 24.0.0、PyTorch 2.12.0。

### 新環境的一次性歷史快照成本

Task worktree 沒有 primary checkout 的 ignored snapshots，因此先補建以下已結束版本 counters。
這不是每輪發布都應支付的成本，最終驗收會沿用已補建的快照。

| 完成階段 | 秒 | 分鐘 |
|---|---:|---:|
| 16.19 勝率 counters／snapshot | 471.97 | 7.87 |
| 16.18 勝率 counters／snapshot | 291.57 | 4.86 |
| 16.19 core-item counters | 421.65 | 7.03 |
| 合計 | 1,185.19 | 19.75 |
