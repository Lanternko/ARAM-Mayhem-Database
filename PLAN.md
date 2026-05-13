# ARAM Win-Rate Prediction — NN 規劃 (v3, pivoted from Mayhem to standard ARAM)

> **目標**:輸入一場 ARAM (queueId=450) 對局的雙方英雄組合 (5v5),輸出藍方獲勝機率。
>
> **鎖定參數:**
> - **Mode**: 標準 ARAM (`queueId=450`, mapId=12 Howling Abyss)
> - **Patch**: 16.9.x (latest as of 2026-05-12)
> - **資料來源**: Riot MATCH-V5
> - **Region**: TW (sea routing for match-v5, asia for account-v1)
> - **Sample 策略**: **從使用者 PUUID 雪球展開**(BFS),非從 SR 高段名單;這對 ARAM-style mode 比較準
>
> **v2 → v3 變更:**
> - **改題**:從 Mayhem (queueId=2400) 改回標準 ARAM (queueId=450)。原因:Riot 從 2025 起在 MATCH-V5 端直接 strip Mayhem matches(見 [riot-developer-relations#1154](https://github.com/RiotGames/developer-relations/issues/1154)),公開 dev key 拿不到任何 Mayhem 資料,連 op.gg / aram.zone 也都沒有
> - **抓取策略**:從 user PUUID snowball,不再從 challenger/diamond 名單;後者在 KR 幾乎不玩 ARAM-style mode
> - 模型架構、評估指標、calibration 流程 **完全不變** — 如未來拿到 Mayhem 資料,只需 fine-tune embedding 層

---

## 1. 問題定義

- **Input**:`team_blue ∈ Champion^5`, `team_red ∈ Champion^5` (兩方各 5 個英雄,無重複)
- **Output**:`P(blue wins) ∈ [0, 1]`
- **任務**:二元分類 (logistic regression + NN feature extractor)
- **評估指標**
  - Log-loss (主指標,訓練目標)
  - Accuracy @ 0.5 (直覺指標)
  - Calibration (ECE) — 預測機率是否誠實 (50% 預測組真實勝率應接近 50%)
  - Brier score
- **Baseline 必跑**
  - 全部預測 50%(log-loss = 0.693, acc = ~50%)
  - 全部預測訓練集藍方勝率(通常藍方略佳,約 50–52%)
  - Logistic regression on one-hot champion presence (+1 blue, -1 red, 0 absent) → ~0.67 log-loss 是已知文獻數字,**任何 NN 沒打過這個就是失敗**

### 〔Assumption 1〕只用「英雄組合」當特徵
不含召喚師段位、符文、出裝、玩家勝率/熟練度。這讓 model 學的是英雄組合本身的協同/克制,而不是「誰玩」。如果想預測「特定召喚師玩特定組合會贏嗎」,就是另一個更難的問題(玩家 embedding + champion embedding + 互動)。

---

## 2. 資料

### 2.1 來源
- **首選**:Riot API `MATCH-V5` (queueId = 450 表示 ARAM)
- **次選**:Kaggle 上的 ARAM dump、社群網站爬蟲(u.gg / aram.zone)— 風險是更新滯後 + ToS

### 2.2 規模目標 〔Assumption 2〕
- **MVP**:50k 對局(高鑽以上,單一 patch,Riot API 抓 1~2 週)
- **完整**:300k+,跨 2~3 個 patch(每 patch 加 patch_id embedding)

> **為什麼要限定 patch**:ARAM 改動很頻繁(每個 patch 都有 dmg_dealt / dmg_taken 的英雄個別調整),跨 patch 直接混訓會讓 model 學到平均值而非當前 meta。

### 2.3 Schema (parquet, 一場一 row)
```
match_id: str          # 主鍵
patch: str             # "14.5"
queue_id: int          # 450 (ARAM)
region: str            # "kr" / "tw" / "na" / ...
duration_sec: int
blue_champions: int[5] # championId,已排序(順序無意義)
red_champions:  int[5]
blue_wins: bool        # label
avg_tier: str          # 可選,後續玩家特徵用
created_at: timestamp
```

**重要**:`blue_champions` / `red_champions` 內部要排序(用 championId 由小到大),因為 ARAM 隊伍內位置無意義,排序避免 model 學到位置 spurious feature。

### 2.4 Split 策略 — **時間切分,不要隨機**
- Train:patch 14.3 ~ 14.4
- Val:patch 14.5 前半
- Test:patch 14.5 後半 + 14.6 holdout(看 model 對「沒看過的 patch」泛化多差)

隨機切會洩漏(同個 meta 的對局)。

### 2.5 資料過濾(**MVP 必做,不是 nice-to-have**)
1. **去重**:`match_id` PRIMARY KEY,跨來源若重複抓到只留一筆
2. **排除 remake / 短局**:`duration_sec < 300`(5 分鐘以下幾乎都是 remake / surrender at 3:00)直接丟
3. **排除 AFK / leaver**:若 raw data 有 participant `leaver` flag 或 `time_played` 顯著 < `duration` 的玩家 > 1 人,整場排除
4. **異常局**:`duration_sec > 3600`(1 小時以上)丟,通常是 bug 或極端 trolling
5. **MMR / region 一致性**:MVP 只取單一 region + 限定段位區間(e.g. Diamond+),記錄在 `data/README.md`,test 集若混 region 要分層報指標

### 2.6 ARAM 特有風險:**Reroll bias**
ARAM 玩家可以 reroll、互換英雄。所以 observed team comp **不是均勻隨機抽樣**,而是「玩家收藏池 + 偏好 + reroll 策略」的條件分佈。這代表:
- 模型學到的「英雄 X 強」可能部分是「會選 X 的玩家通常較強/較熟」
- 我們無法完全消除這個 bias(沒有玩家 ID 特徵),但要 **在報告中明說**:輸出機率是「在玩家自然 reroll 行為下的條件勝率」,不是「強制這 10 隻打的勝率」
- 若未來要去 bias,需要加玩家特徵(超出 MVP 範圍)

### 2.7 Blue-side base rate 監控
ARAM 藍方歷史略佔優(50–52%)。每個 split 都要記錄 `mean(blue_wins)`,確認沒有 split artefact。Constant baseline 用該 split 的 base rate,而非 0.5。

### 2.8 Combo overlap 指標(test 評估必看)
每個 test match 計算:
- `n_unseen_pairs_in_blue`:藍方 10 個同隊 pair 中,training 從未一起出現的數量
- 同理算紅方、跨隊 pair
- 報告分組指標(low / mid / high unseen pair count 的 log-loss),看 model 對「沒見過的組合」泛化如何

---

## 3. Model 架構 — 三層階梯

### 3.1 Tier 0:Logistic Regression (baseline,**必跑**)
- Feature:長度 = `2 × N_champions` 的稀疏向量,藍方位置 = 1、紅方位置 = 1
- 用 sklearn,15 分鐘搞定。所有 NN 都跟這個比。
- **此模型沒有「組合互動」概念,所以 NN 的提升空間就是組合互動學得多好。**

### 3.2 Tier 1:Champion Embedding + DeepSets (主推 MVP) — **revised**
```
champion_id → Embedding(D=32)
sum_blue = Σ embed(c) for c in blue           # set invariance
sum_red  = Σ embed(c) for c in red
diff     = sum_blue - sum_red                 # antisymmetric channel
total    = sum_blue + sum_red                 # symmetric channel (shared context)

# 兩個 head 分別吃 antisym/sym 訊號,輸出時加總並只在 antisym 那支翻號維持 swap symmetry
h_diff  = MLP_A([D] → 64 → 32)(diff)          # 對 swap 取負
h_total = MLP_S([D] → 64 → 32)(total)         # 對 swap 不變(會被閘控)
logit   = w_A · Linear(h_diff) + g(h_total) · Linear(h_diff)
        ≈ Linear(h_diff) * (1 + small_gate(h_total))
```
- 關鍵想法:**logit 必須對 swap 反對稱**,所以 `h_total` 不能直接加進 logit,只能當「給 antisym 訊號的乘性 gate」(乘以反對稱東西仍是反對稱)
- 若 gate 設計嫌複雜,**v1 MVP 簡化版**:`logit = Linear(MLP_A(diff)) + ε·Linear(MLP_A(diff) ⊙ MLP_S(total))`,實作上就一個 MLP 吃 `[diff, total]` 再強制最終 logit 對 swap 反對稱 → 用 `f(diff, total) - f(-diff, total)`(訓練時跑兩次 forward,或 batch double)
- **保留:** set invariance(sum)、swap antisymmetry(經 head 設計強制)
- **修補:** v0 純 `diff` 丟掉兩隊共有上下文(Codex 反饋)。例如 blue=[A,B,C,D,E] vs red=[A,B,C,D,F],v0 的 `diff = emb(E)-emb(F)` 看不到 A~D 背景;v1 的 `total` 保留 A~D~E~F 的整體訊號
- 參數量仍 ~200k 等級,CPU 訓得動

### 3.3 Tier 2:Pairwise Interaction (如果 Tier 1 沒贏 LR 多少)
顯式建模英雄兩兩互動(同隊協同 + 跨隊克制):
```
synergy_blue = Σ_{i<j in blue} f_syn(emb_i, emb_j)
synergy_red  = Σ_{i<j in red}  f_syn(emb_i, emb_j)
counter      = Σ_{i in blue, j in red} f_cnt(emb_i, emb_j)
logit = MLP(concat[synergy_blue - synergy_red, counter])
```
- `f_syn` 對 (i,j) 對稱、`f_cnt` 對 (i,j) 反對稱
- 計算量大 25× 但仍非常便宜(每場 10+25 = 35 個 pair lookup)

### 3.4 Tier 3:Transformer — **降級**
保留為「資料 >> 300k 且 Tier 2 plateau」才考慮的選項,本規劃不細寫。

### 〔Assumption 3〕從 Tier 1 開始,跑通才往 Tier 2 走
不要一開始就上 Transformer。

---

## 4. 訓練流程

### 4.1 Loss & Regularization
- BCE with logits(**不用 label smoothing** — 校準改後處理)
- Weight decay 1e-4

### 4.2 Optimizer
- AdamW, lr=3e-3 (small model 可以激進)
- Cosine schedule, 1 epoch warmup
- Batch 1024

### 4.3 Augmentation —「Swap teams」(consistency,不是 2× 資料)
每個 batch 50% 機率 swap + label flip。因為 Tier 1 已硬編碼 symmetry,**這是 consistency regularization 而非真正資料擴增**,效果有限但近乎免費,留著。

### 4.4 Calibration — **post-hoc temperature scaling**
訓練用純 BCE 即可。訓練完在 **val set** 上學一個 scalar `T`,把 `p = σ(logit / T)` 作為輸出機率。再在 test 上量 ECE / Brier。比 pre-hoc label smoothing 乾淨,且 ECE 通常可降 50%+。

### 4.5 Early stopping
看 val log-loss,patience=5 epoch。

### 4.6 框架 〔Assumption 4〕— **revised**
**純 PyTorch**(Lightning 對這個 model size 是過度抽象)。
資料用 `polars` 處理(parquet 快、API 比 pandas 清爽)。
Logging 用 `wandb` 或乾脆 stdout + csv。

---

## 5. 預期數字(用來判斷 model 是不是壞了)

| Model | Val log-loss | Val acc | 註 |
|---|---|---|---|
| All blue-base-rate (~0.51) | ~0.693 | ~51% | constant baseline,**必跑** |
| LR (one-hot ±1) | 0.68 ~ 0.69 | 53 ~ 55% | 已知文獻數字,NN 必須贏這個 |
| Tier 1 DeepSets (`[diff,sum]`) | 0.67 ~ 0.68 | 54 ~ 57% | MVP 目標 |
| Tier 2 Pairwise | 0.66 ~ 0.67 | 55 ~ 58% | 若 Tier 1 沒打過 LR 才上 |

**警戒線:**
- **acc > 65% → data leak**,立刻檢查 split、match_id 去重、是否誤抓 post-game data
- **val log-loss < train log-loss → split 設定錯誤**(通常是時間切顛倒)
- ARAM 本質高 variance,人類專家看牌面也只能猜 60% 左右

---

## 6. 專案結構

```
aram-winrate-nn/
├── PLAN.md                  # 本檔
├── pyproject.toml
├── data/
│   ├── raw/                 # Riot API 回傳的 raw JSON
│   ├── processed/           # parquet
│   └── README.md            # schema + 抓取日期
├── src/aram_nn/
│   ├── ingest/
│   │   ├── riot_client.py   # rate-limit aware
│   │   └── extract.py       # raw JSON → parquet schema
│   ├── data.py              # Dataset, DataLoader, split
│   ├── models/
│   │   ├── logreg.py        # sklearn baseline
│   │   ├── deepsets.py      # Tier 1
│   │   └── pairwise.py      # Tier 2
│   ├── train.py             # Lightning trainer
│   ├── eval.py              # log-loss, ECE, calibration plot
│   └── infer.py             # CLI: 給 10 個 champ name → 勝率
├── notebooks/
│   └── 01_eda.ipynb         # 英雄出場/勝率分佈
└── tests/
    ├── test_symmetry.py     # swap teams → 1-p 確認
    └── test_set_invariance.py # 隊內 shuffle → 一樣輸出
```

---

## 7. 里程碑

1. **M1 (data, 1~2 天)**:Riot API key、抓 5k 對局、parquet 落地、EDA notebook
2. **M2 (baseline, 半天)**:LR baseline 跑通,確立評估腳本
3. **M3 (Tier 1, 1 天)**:DeepSets 訓練 + symmetry/invariance test 過
4. **M4 (Tier 2, 1~2 天)**:Pairwise interaction,如果 Tier 1 沒打過 LR 才需要
5. **M5 (infer CLI)**:`aram-nn predict <c1> <c2> ... <c10>` → 勝率 + 信心區間

---

## 8. 我自己想被審查的 4 個點(給 Codex 看)

1. **Tier 1 的 `diff = blue_sum - red_sum` 是不是太破壞資訊?** 例如 blue=[A,B,C,D,E] 跟 red=[A,B,C,D,F] 的 diff 只剩 emb(E)-emb(F),完全看不到 A~D 之間的 synergy。是不是該保留 `[sum_blue, sum_red]` 或 `[sum_blue+sum_red, sum_blue-sum_red]`(後者保 symmetry)?
2. **Patch 處理**:加 `patch_id` embedding 還是每個 patch 訓一個 model?前者 sample-efficient,後者乾淨但小 patch 資料不夠。
3. **Calibration**:小 NN 通常會 overconfident,要不要在訓練後加 temperature scaling on val set?
4. **資料數量 50k 對 D=32 embedding (~170 champs × 32 = 5.4k params) + MLP 是否合理?** 還是 D 該降到 16?

---

## 9. 〔Assumption 〕總覽(請挑要改的)

| # | 假設 | 替代方案 |
|---|---|---|
| 1 | 只用英雄組合特徵 | 加段位 / 玩家熟練度(更難但更準) |
| 2 | 50k MVP / 單 patch | 5k 練流程 / 500k 跨 patch |
| 3 | Tier 1 起步 | 直接上 Transformer(不建議) |
| 4 | PyTorch + Lightning | 純 PyTorch / JAX / TF |
