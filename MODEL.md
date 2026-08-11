# Modeling and Experiment Policy

## Purpose

本專案的研究問題是：只知道雙方五名英雄時，能否穩定產生比藍方 base rate 更有資訊、且校準良好的勝率估計。現行產品資料域是 Mayhem（`queueId=2400`）；標準 ARAM（`queueId=450`）是可重用的比較資料域，不可與 Mayhem 無條件混訓。

模型輸出是藍方獲勝機率，不是單純的勝負標籤。產品使用情境包括英雄 tier、組合比較與 draft recommendation，因此 log loss、calibration 與資料可追溯性和 accuracy 同等重要。

## Scope and ownership

- 本檔是現行模型方向、資料切分與 promotion gate 的唯一 canonical 文件。
- `notes/agents/experiments-and-data.md` 定義每次實驗必備的 manifest、變因控制與報告格式。
- `PLAN.md`、`notes/archive/MODEL_NOTES.md`、`notes/archive/TODO.md` 是歷史設計與實驗快照，不可拿來覆蓋本檔。
- 執行指令只放在 `scripts/README.md` 與各 CLI 的 `--help`；本檔只定義決策規則。

## Data contract

- Mayhem 的合法來源是本機 LCU。公開 Riot API 的 standard ARAM 資料不能冒充 Mayhem。
- Exact match identity 一律是 `game_id`。英雄組合 hash 只能用於分析，不可用於 exact dedupe。
- 每隊英雄在持久化或建模前按 `championId` 排序；ARAM/Mayhem 沒有可泛化的位置語意。
- Train/validation/test 依 `game_creation_ms` 時間切分；禁止 random split，避免同一 meta 期間跨集合洩漏。
- 跨 patch 訓練必須加入 patch feature 或逐 patch 報告；否則只在單一 patch 內比較。
- Dataset 必須記錄 queue、patch、時間範圍、row count、去重規則、來源 DB fingerprint 與切分邊界。
- 任何可能從結果或賽後狀態推回 label 的欄位都不得進入 composition-only benchmark。

## Baseline ladder

實驗依序回答「是否真的增加資訊」，而不是直接追求更大的模型：

1. Constant baseline：只預測 train set 藍方勝率。
2. Champion-strength baseline：每名英雄的線性效果。
3. Composition baseline：加入雙方聚合與可解釋交互作用的 logistic regression。
4. Structured neural model：只有在相同資料、相同 split、相同評估下超過強 baseline 才成立。

LR baseline 必跑。若神經網路只提高 train 指標、未改善時間外 test log loss 或 calibration，就不 promotion。

## Model invariants

- Team swap 必須反對稱：`logit(blue, red) = -logit(red, blue)`。
- 聚合輸入至少保留 `diff = blue - red` 與 `total = blue + red`；只有 diff 會丟失雙方共有的 composition context。
- Set model 必須對隊內英雄排列 permutation invariant。
- Calibration 使用 validation set 的 post-hoc temperature scaling；不加 label smoothing，避免 ECE 解讀混亂。
- Accuracy 明顯高於合理區間（例如 composition-only 超過 65%）先視為 leakage signal，不視為突破。

## Experiment control

一次實驗只改一個主要變因。資料集、split、seed、特徵、模型、loss、訓練預算與 calibration 中，未被指定為 treatment 的項目都要固定並記錄。

每組比較至少包含：

- 明確 hypothesis 與預期可被否證的結果。
- Canonical baseline 與 treatment 的唯一差異。
- 固定 dataset manifest、time split 與 evaluation code。
- 至少三個 seed，或解釋為何 deterministic estimator 不需要 seed distribution。
- Accuracy、log loss、Brier score、ECE，以及相對 constant/LR baseline 的 delta。
- 依 patch、時間、樣本量與常見／長尾英雄組合切片的 failure analysis。

完整執行模板與 promotion gate 見 `notes/agents/experiments-and-data.md`。

## Promotion gate

新模型要成為產品或後續研究 baseline，必須同時滿足：

- 在 untouched time-based test set 上改善主要指標；主要指標預設為 log loss。
- 改善不是單一 seed、單一 patch 或少數高頻英雄造成。
- Swap、permutation、dedupe、privacy 與 calibration 檢查通過。
- 推論所需欄位在實際產品資料流中可取得，且不引入 label leakage。
- Artifact、config、dataset manifest 與結果摘要可重現。

不滿足 gate 的結果仍可保存為 evidence，但要標記 rejected、inconclusive 或 exploratory，不可改寫成 current direction。

## Evidence map

- `notes/issue-16-analysis.md`：特定研究問題的分析與限制。
- `documents/reports/composition_analysis_handoff_2026_06_11.md`：composition 實驗證據補充。
- `documents/reports/final_project_report_2026_06_14.md`：2026-06 的 final report 快照。
- `notes/archive/README.md`：歷史模型、產品與設計文件索引。

數字結論以綁定 dataset manifest 的報告為準；本檔只保存不隨單次跑次變動的決策規則。
