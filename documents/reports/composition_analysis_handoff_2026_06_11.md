# 交接文件 — Final Project 投影片 + Composition Signal 分析

> **Evidence supplement:** 本檔綁定 2026-06-11 的資料集與 split，保存 composition 實驗證據；不是現行模型 policy owner。Current policy 見 `../../MODEL.md`。

**日期:** 2026-06-11
**範圍:** Open Slide final-project deck 擴充(10→14 頁)+ 7 個 composition-signal ablation
**主資料源:** `data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet`(queue 2400 / Mayhem,patch 16.10)
**標準切分:** 215,406 train / 46,158 val / 46,158 test(time split,完全重現 `final_project_text_report_2026_05_31.md`)

> ⚠️ 重現 split 一定要用 `..._2026_05_25_live.parquet`。`mayhem_lcu_ml_compare_latest.parquet` 只有 121k 場,給出不同的小 split。

---

## 1. 一句話結論

**英雄身份(champion identity)幾乎是一個充分統計量。組合特徵、學到的 synergy、NN 的額外容量,平均而言都只加一點點 —— 因為大多數 draft 本來就平衡。價值集中在「極端組成」的尾巴:那裡 composition 值 ~4 個勝率點,推薦器也在那裡才真正發揮。模型的價值是集中的,不是均勻分布的。**

可直接當口頭講稿(英文):
> *"Champion identity is a near-sufficient statistic. Everything else adds little on average, because most drafts are balanced. The exception is the unbalanced tail, where composition is worth ~4 win-rate points and where the recommender earns its keep. The model's value is concentrated, not spread."*

---

## 2. 投影片現況(14 頁)

**檔案:** `documents/open-slide-final-project/slides/aram-mayhem-winrate/index.tsx`(open-slide / Vite / React TSX)
**Dev server:** 使用者自己跑在 `http://127.0.0.1:5173/s/aram-mayhem-winrate`(demo 頁 = `?p=12`)
**驗證用 preview:** `.claude/launch.json` 有 `open-slide-final-project` 配置,釘在 port 5174(Chrome MCP 的瀏覽器在遠端 mac,連不到本機 localhost;驗證請用 Claude Preview 工具,且偶發 screenshot timeout 時改用 `preview_eval` 量 DOM)。

**頁序(footer 頁碼):**

| # | 元件 | 內容 | 本次改動 |
|---|---|---|---|
| 1 | Cover | 標題 + 307K/58.27%/+6.36pp | 統一 benchmark 口徑:307K = Patch 16.10 benchmark matches |
| 2 | Question | **勝率因素 + 可控變量**(player skill / in-game variance / champion composition) | 重寫:移除 dataset/split 資訊,聚焦 control lever |
| 3 | WhatFromComp | **team comp 三層 signal 圖**:input 10 champion IDs → who/shape/fit | 重寫:取代三張文字卡,改成更直觀流程圖 |
| 4 | Data | **LCU 資料採集:priority queue + BFS + dataset size** | 重寫:左側 4-step BFS,右側只留 307,722 場與 70/15/15 切分 |
| 5 | Leakage | dedupe by game_id / sort champions / time split | — |
| 6 | Features | one-hot LR 編碼 + **Champion LR 為何強** + composition feature pills | 補 one-hot 與 champion identity 暗含資訊 |
| 7 | Model | DeepSets 架構圖 | — |
| 8 | Benchmark | **模型對比 bar + Champion LR 強 baseline + composition residual signal** | 補 Champion LR 強的原因與 +0.6pp / +4.4pp 解讀 |
| 9 | Results | 強隊/弱隊分離(62.3% / 71.1%) | — |
| 10 | Metrics | calibration 橫條圖 | — |
| 11 | Interpretation | 「58% 有意義因為任務 noisy」 | — |
| 12 | Demo | **真實工具截圖**(選角推薦 + 進場 5v5) | 全新頁,截圖已打碼 |
| 13 | WhySwap | **通用語言示意圖**(五攻擊者一傷害類型) | 全新頁 |
| 14 | Conclusion | **A draft guide, not a match oracle**:一句主結論 + 307K/58.27%/+4.4pp | 重寫:取代抽象三點條列 |

**Demo 截圖資產:** `slides/aram-mayhem-winrate/assets/demo-champ-select.png`(1764×975)、`demo-ingame.png`(848×604)。
- 來源:使用者在對話貼的螢幕截圖,從 session transcript 的 base64 還原(webp→png)。
- **已打碼**:選角圖左側 5 行玩家暱稱 + 左下聊天記錄(含 Riot ID tagline)以馬賽克處理;英雄名稱保留。進場圖只有英雄名、未處理。
- 若要換真圖,覆蓋同名檔即可,HMR 會更新。

**觀眾約束(寫投影片務必遵守):** 口頭報告、講英文;觀眾是有 ML 知識但不一定懂 LOL 的同學 → 解釋頁只能用通用遊戲語言(tank / mage / physical-magic damage / crowd control),不可出現英雄名或 LOL 術語。

---

## 3. 本次跑的 7 個分析

全部腳本 self-contained、可重跑,reuse `train_composition_lr` / `analyze_composition_signals` / `train_ability_nn` 的 featurization,確保與 benchmark 同 split 同特徵。輸出在 `outputs/*.json`(2026-07-06 整理後,已產生的 ablation_*.json 搬進 `outputs/ablation/`;腳本預設 `--out` 仍寫回 `outputs/` 根目錄,重跑後要再搬一次)。

| # | 問題 | 腳本 | 輸出 | 核心數字 |
|---|---|---|---|---|
| 1 | 組合價值 by 傷害組成區間 | `ablation_composition_subset.py` | `ablation_composition_subset.json` | 全體 +0.6pp;≥80% AD 尾巴 **+4.4pp**(n=867);win-rate table = 56.94% |
| 2 | 英雄×英雄 synergy 能 out-of-sample 嗎 | `ablation_pair_synergy_persistence.py` | `..._pair_synergy_persistence.json` | 每對 ~293 場;corr(train,test)=**+0.17**;top-20 +4.35→**+0.50**(留 11%) |
| 3 | 改成英雄×職業呢 | `ablation_champ_role_persistence.py` | `..._champ_role_persistence.json` | 每格 ~8349 場;corr=**+0.37**;top-20 +1.92→**+1.15**(留 60%);個案(Caitlyn×Tank、Naafiri×Assassin)不重現 |
| 4 | 可加 vs 非加性特徵 | `ablation_additive_vs_nonadditive.py` | `..._additive_vs_nonadditive.json` | +可加 = **−0.011pp**;+非加性 = **+0.555pp**(吃掉 92% gain) |
| 5 | 模型差異顯著嗎(Q1) | `ablation_recommender_backtest.py`(Part A) | `..._recommender_backtest.json` | 配對差 **+0.545pp,CI [0.247, 0.845]**,P>0.999;各自 accuracy CI 大幅重疊 |
| 6 | 推薦器 test 上有效嗎(隨機隊) | `ablation_recommender_backtest.py`(Part B) | 同上 | full top-bottom **+5.8pp**;strength-only +7.5pp;組合 added **≈0(−1.7pp,噪音內)** |
| 7 | 尾巴隊伍呢 | `ablation_recommender_backtest.py --kept-extreme 0.22` | `..._recommender_backtest_tail.json` | full **+13.0pp**;strength +8.0pp;組合 added **+5.0pp**(翻正) |

### 統一圖像(組合的價值 by regime)

| 量測 | 全體隊伍 | 極端組成尾巴 |
|---|---|---|
| accuracy(組合 vs 純英雄) | +0.6pp(顯著 [+0.25, +0.85]) | +4.4pp(≥80% AD) |
| 推薦 spread(組合 added) | ≈0(−1.7pp) | **+5.0pp** |

**機制鏈:**
- 英雄身份把**所有可加**組合(前排總和、職業 count、score 加總)吃光 → 加進去 +0.0(實驗 4)。數學上 `Σ attribute(c)` 落在英雄 one-hot 張成空間內。
- 只有**非加性**(AD 比例、true 比例、lacks 門檻、交互)帶新訊號 → +0.55(實驗 4)。
- 非加性效應在平衡隊極小、在尾巴才咬得動 → 解釋實驗 1 的 +0.4(bulk)vs +4.4(tail)。
- Synergy:英雄×英雄 = 噪音(實驗 2);英雄×職業 = 真但小、~1pp,門檻不可估(實驗 3)。**負向(冗餘/反 synergy)一律比正向(配合加成)持久** —— 三個實驗都出現。
- 推薦器:隨機隊靠英雄強度(組合稀釋成噪音);尾巴隊組合補洞翻正(實驗 6 vs 7)。**這跟 demo 推 TF 給全 AD 隊完全一致 —— demo 就是尾巴。**

### Q1 額外結論(使用者問「NN 信賴區間較窄較穩?」)— 方向相反

- accuracy CI 寬度由 (n, p) 決定,兩模型幾乎一樣寬(±0.45pp),**NN 的 accuracy CI 不會較窄**。
- 判模型差異要用**配對**檢定(同 test、大多同答案),不是看 CI 是否重疊;配對 CI [+0.25, +0.85] 不含 0。
- 「穩」若指機率校準:Composition LR ECE=0.0026 vs DeepSets+scores ECE=0.0192(**NN 差 7 倍**)。
- run-to-run:LR 凸優化解唯一;NN 有 SGD seed 噪音,更不穩。
- → deck 若主打 NN,正確理由是「可持續吃進更多特徵的框架」,不是「更穩」。穩定性這票其實投給 LR。

---

## 4. 重現方式

```powershell
# 全部用同一份資料、同 split,直接跑(.venv 已有 numpy/sklearn/polars)
.venv\Scripts\python.exe scripts\ablation_composition_subset.py
.venv\Scripts\python.exe scripts\ablation_pair_synergy_persistence.py
.venv\Scripts\python.exe scripts\ablation_champ_role_persistence.py
.venv\Scripts\python.exe scripts\ablation_additive_vs_nonadditive.py
.venv\Scripts\python.exe scripts\ablation_recommender_backtest.py                         # 隨機隊
.venv\Scripts\python.exe scripts\ablation_recommender_backtest.py --kept-extreme 0.22 `
    --n-boot 200 --sample-teams 5000 --out outputs\ablation_recommender_backtest_tail.json # 尾巴隊
```

**實作備註(給未來重看腳本的人):**
- `rec_score` 慣例 = c* 贏過的候選比例,1.0 = c* 是首選(早期版本寫反過、符號顛倒,已修)。
- backtest 隱藏的是**隨機**位置的英雄(英雄已按 championId 排序,固定隱藏會有位置偏差)。
- strength-only baseline 用「同一個 comp_model 的英雄子 logit」(`coef_[:n_champs]`),不是另一個模型 → 乾淨分解,差異純粹來自 composition 特徵。
- synergy 定義 = 超出 additive Champion LR 預測的殘差勝率(blue 側 +residual、red 側 −residual)。

---

## 5. Open items(尚未做 / 已提議)

1. **附錄頁(已提議,未動手):** 在正式 14 頁後加 1–2 張 appendix —
   - 「Where composition matters」2×2 表(全體 vs 尾巴 × accuracy vs 推薦)。
   - speaker notes 塞三個 Q&A 殺手數字:配對 CI [+0.25,+0.85]、synergy 持久度 r=0.17、可加 +0.0 vs 非加性 +0.55。
   對應最可能被問的三題(差異顯著嗎 / 為何不學 synergy / 為何 composition 只加一點)。
2. **Test A(較重,未做):** 把**真 DeepSets** 拉進 CI 比較 —— 重訓 NN 跑配對 bootstrap + seed variance,正面證伪「NN 更穩」。目前 Q1 用 Composition LR 當可跑替身。需要 NN checkpoint(`models/` 是 .gitignored,在 parent repo;worktree 內看不到,要顯式傳 `--checkpoint`)。
3. **個案深究(可選):** Caitlyn×Tank、Naafiri×Assassin 在 train 漂亮、test 蒸發 —— 若要在報告講「個案不可信」可保留當反例,不需再跑。

---

## 6. 檔案索引

**新增腳本:** `scripts/ablation_composition_subset.py`、`ablation_pair_synergy_persistence.py`、`ablation_champ_role_persistence.py`、`ablation_additive_vs_nonadditive.py`、`ablation_recommender_backtest.py`
**輸出:** `outputs/ablation/ablation_*.json`(5 個,recommender 有 base + `_tail`;2026-07-06 起搬進 `outputs/ablation/` 子目錄)
**投影片:** `documents/open-slide-final-project/slides/aram-mayhem-winrate/index.tsx` + `assets/demo-*.png`
**原始報告:** `documents/final_project_text_report_2026_05_31.md`(模型對比表的數字來源)
**記憶:** `~/.claude/projects/.../memory/project_final_slides.md`(deck 位置 + 觀眾約束)

**關鍵基準數字(報告 held-out test):**
| 模型 | acc | log_loss | ECE |
|---|---|---|---|
| Constant baseline | 51.91% | 0.6924 | 0.0012 |
| Win-rate table(no ML) | 56.94% | — | — |
| Champion LR | 57.34% | 0.6767 | — |
| Composition LR | 57.94% | 0.6741 | 0.0026 |
| DeepSets(IDs only) | 57.88% | 0.6743 | 0.0194 |
| DeepSets + scores | 58.27% | 0.6724 | 0.0192 |
