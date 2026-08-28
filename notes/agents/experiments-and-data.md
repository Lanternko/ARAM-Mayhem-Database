# Experiments and Data — 實驗、變因控制與資料品質

這是所有模型、統計、crawler policy、推薦與產品資料實驗的第二層 contract。根層決策原則見 `AGENTS.md`；具體腳本從 `scripts/README.md` 與各 CLI `--help` 找，不在這裡維護另一份命令清單。

## 1. 先定義 decision，不先跑模型

每次實驗開始前先寫一個最小 manifest；沒有 manifest 的結果只能當探索：

| 欄位 | 必須回答 |
|---|---|
| `decision` | 結果會改哪個 default、功能、門檻或研究結論？ |
| `hypothesis` | 預期方向、理由與可能失敗的 regime 是什麼？ |
| `unit` | 一場 game、一位 player、一個 champion×augment cell，還是一次 session？ |
| `treatment`／`control` | 唯一主要差異是什麼？ |
| `scope` | queue、patch、region、time cutoff、來源與 exclusions。 |
| `split` | train／val／test 的時間邊界與 row count。 |
| `primary_metric` | 哪一個指標決定是否 promotion？ |
| `guardrails` | 哪些 calibration、coverage、latency、privacy 或產品指標不能退？ |
| `promotion_rule` | 多大 effect、多少 uncertainty、哪些測試通過才改 default？ |

Manifest 可放在 experiment JSON 的 `meta`、輸出旁的 Markdown，或長期報告開頭；重點是結果出來後不可改問題、primary metric 或 exclusion 來追漂亮數字。

## 2. Dataset contract

每個可引用結果至少保存下列 lineage：

- dataset path／DB path、檔案大小與 mtime；若資料會被覆寫，保存 immutable export、snapshot ID 或明確 capture cutoff。
- queue、完整 patch prefix、region、最早／最晚 `game_creation_ms`、原始 rows、過濾後 rows、各 exclusion 數量。
- exact dedupe key=`game_id`；不要以 composition hash 去重。
- label definition、feature availability time 與所有衍生 feature 版本。任何 match 結束後才知道的欄位不得進 pre-game model。
- train／val／test 的時間範圍、各自 blue base rate、unknown champion drop count 與 combo overlap 指標。
- code commit、script、完整 arguments、random seeds、依賴環境與輸出位置。

不要用 `*_latest.parquet` 當唯一身份；它可能在兩次實驗間指向不同 row set。若只能讀 live `games.db`，先記 cutoff／row count 並避免在長實驗中讓資料悄悄跨過 split boundary。

## 3. Record quality gates

一場 match 進分析前必須滿足：

1. `game_id` 存在且在 dataset 中唯一。
2. queue 與 patch 可辨識，caller 已套明確 scope。
3. 10 participants、team 100／200 各 5 人、win flag 完整。
4. `blue_champions`／`red_champions` 各 5 個並按 `championId` 升序；4310 先用 `base_champion_id()` 正規化 metadata join。
5. 時間、duration 與必要 participant JSON 能解析；corrupt optional JSON 可以降級為缺值，但必須計數，不可靜默換成看似真實的 0。
6. Public export 不含 PUUID、summoner name、Riot ID 或 UUID-like identifier。

新分析優先用 `src/aram_nn/gamedata.py`。自行手刻 SQL 只有在 canonical loader 無法表達查詢時才合理，且必須重現 queue／patch／participant semantics 的測試。

## 4. 控制變因

- 模型 A／B 使用相同 game IDs、相同 chronological split、相同 feature normalization、相同 label 與相同 evaluation code。
- 比較新增 feature 時，control 是同一模型拿掉該 feature；不要同時換 dataset 或 optimizer。
- 比較資料策略時，固定 model／training budget；比較 model 時，固定 dataset／split／feature availability。
- 比較 crawler policy 時，stable-hash 指派 experimental arm，避免 session、時間或 seed family 系統性偏到某組；報 captures／request、target games／player、queue yield 與 wall-clock，不只看 done count。
- 比較推薦器時，固定候選池與可見資訊；同一局上的 treatment-control 差做 paired evaluation。隨機隊與 composition tail 分開報，因效果可能只在極端 regime 出現。
- 多變量不可避免時，使用明確 factorial design 或逐層 ablation，並承認 interaction；不要從整包改動回推單一原因。

## 5. Split、baseline 與 leakage

- 依 `game_creation_ms` 排序做時間切分；random split 只可用於不涉及 meta 泛化的局部 smoke test，且不可引用為產品 evidence。
- Test set 在 hypothesis、feature、threshold、seed 與 model class 固定後才看一次。需要多輪決策時改用 expanding-window validation 或新時間窗。
- 每個 split 記 blue base rate；constant baseline 使用該 scope 的 rate，不假設固定 0.5。
- Baseline ladder：constant → champion identity LR → composition LR → DeepSets／interaction model。每一層只宣稱相對最近合理 baseline 的增益。
- Champion one-hot 已隱含大量可加 team attribute；新增 frontline／damage／role sum 若落在相同線性空間，沒有新資訊是合理結果。真正 composition signal 通常來自 ratio、threshold、missing capability 與 interaction。
- `accuracy >65%`、val 顯著優於 train、跨 split 有重複 `game_id`、或使用 post-game participant stats，任一項都先視為 leakage audit trigger。

現行模型不變量與 promotion policy 見根目錄 `MODEL.md`。`PLAN.md` 只保留 swap-team antisymmetry、`[diff,total]` channel 與 temperature scaling 的歷史推導；其中資料 patch 與規模不可直接複製為 current scope。

## 6. Metrics 與 uncertainty

模型至少報：

- accuracy（sanity／可溝通，不是唯一選模指標）；
- log loss（機率品質的 primary candidate）；
- Brier score／ECE 與 calibration buckets；
- 每個 split 的 base rate、sample size 與 coverage；
- 產品相關 decision uplift，例如相同候選池的 top-bottom、推薦前後 paired delta。

相同 matches 的模型比較優先報 paired delta 與 paired bootstrap CI。NN 至少多 seed，分開呈現 dataset uncertainty 與 seed variance；不要以最好的 seed 代表模型。小 subgroup 必須報 `n` 與 interval，並把 subgroup discovery 與 confirmatory evaluation 分到不同資料窗。

## 7. 公開統計品質

- Hero、augment、item 與 pair 排名不得直接以 raw WR 排低樣本。使用 sample floor、相對 pick floor、Bayesian／empirical-Bayes shrinkage與 confidence-aware score。
- Hero×augment 應相對該 hero baseline；否則會把英雄本身強度誤認為 augment lift。
- Previous-patch prior 只用來穩定 early patch，必須保存 current games、borrowed share 與來源 patch；current patch 成熟後要讓當期資料接管。
- 排名 score、display WR 與 raw WR 可以服務不同目的，但 payload／tooltip 必須可追溯，不能讓使用者把 shrunk estimate 誤認成原始比例。
- 版本變動榜同時要求 current／baseline sample floor，並對差值做 uncertainty control；不能只挑 raw delta 最大者。

相關常數與實作以 `scripts/tierlist_engine.py` 為準；公開方法說明由 `scripts/tierlist_render.py` 生成。

## 8. Promotion checklist

結果升級成 default、網站指標或研究結論前確認：

- [ ] Manifest 在看 test 前已定義，且 experiment unit／treatment／control 無歧義。
- [ ] Dataset lineage、scope、row counts、split boundaries 與 exclusions 可重建。
- [ ] Treatment-control 除主要變量外保持一致，或已用 ablation 拆解。
- [ ] Baseline 完整；primary metric 改善且 guardrails 未退化。
- [ ] Effect size 附 `n`、CI／seed variance 與 failure regime。
- [ ] 相關 invariants／unit tests 通過；沒有 PII、post-game leakage 或 queue／patch 混用。
- [ ] JSON／CSV／report 保存到 `outputs/<category>/` 或 `documents/reports/`，包含 code commit 與 arguments。
- [ ] 結論寫成證據支持的強度；觀察性資料不使用因果語氣。

代表性歷史分析可參考 `documents/reports/composition_analysis_handoff_2026_06_11.md`，但引用數字前要核對其 pinned dataset、patch 與 split，不可把舊 benchmark 當現況。
