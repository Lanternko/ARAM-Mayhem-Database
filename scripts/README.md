# scripts/ 索引

99 個腳本平鋪、不分子目錄 —— import 耦合（尤其 ablation_*/train_* 之間 `sys.path.insert` 互相 import）讓實體搬移風險 > 收益，所以用這份索引取代資料夾分類。**凍結腳本（見 CLAUDE.md）不要拆分、不要搬、不要互相 import 新程式碼**；新的共用邏輯一律進 `src/aram_nn/`。

## 1. 站台 build（tier-list 網站生成/發布）
- `build_tier_list.py` — Tier-list 網站 build CLI 入口（re-export tierlist_engine + tierlist_render）
- `tierlist_engine.py` — 勝率/augment/affinity/cluster 計算引擎
- `tierlist_render.py` — HTML render + `--shell-only` 快速預覽 + OG/favicon 圖產生
- `templates/site.css` + `templates/site.js` — 站台 CSS/JS 模板，`tierlist_render.py` 讀檔注入
- `champion_roles.py` — 英雄職業（role）對照表，Mayhem 專用
- `publish_static_site.py` — 靜態站台自動發布 CLI（wraps `static_publish_cli`，`--patch-prefix auto` 每 cycle 重解析）
- `site_api.py` — FastAPI backend 入口
- `sync_site_backend.py` — 本機對局同步到 backend API CLI（`--watch` 每 +10k games 推一次）
- `tier_list.py` — 舊版 LR-solo 權重抽英雄 tier list（CSV 輸出，非現行站台管線）
- `build_augment_category_editor.py` — 產生 `augment-category-editor.html`（手動修正 augment 分類 → `scripts/augment_category_overrides.json`）

## 2. 資料收集 harness（LCU collector / watchdog / snowball / overwolf）
- `lcu_collector.py` — LCU collector 主 CLI（collect/snowball/export/dataset/stats 等 subcommand，凍結）
- `mayhem_lcu_watchdog.py` — Windows 版 collector watchdog（排程任務 `MayhemLCUWatchdogKeepalive` 用）
- `watchdog_keepalive.ps1` — 每分鐘檢查/啟動 watchdog（PowerShell，排程任務用）
- `watchdog_keepalive_hidden.vbs` — 無視窗版 keepalive，呼叫 `watchdog_keepalive.ps1`
- `crawler_mac.py` — macOS 版 LCU crawler watchdog（wraps `lcu_collector`）
- `watchdog_mac.py` — macOS crawl watchdog 入口（wraps `crawler_mac`）
- `lcu_backfill.py` — 補抓近期 LCU 對局回填 games.db
- `lcu_dump.py` — 印出 LCU 原始對局資料供診斷
- `lcu_probe_endpoints.py` — 探測 LCU API 找 10 人陣容欄位（推測）
- `lcu_save_eog.py` — 抓當前對局結算畫面存進 games.db
- `dump_champ_select.py` — 存 champ-select session payload 供欄位檢查
- `mayhem_overlay.py` — 遊戲內 F8 疊層，即時評分 augment 選項
- `overwolf_augment_bridge.py` — Overwolf app 本機橋接伺服器（讀 augment 事件）

## 3. 訓練（train_*.py / auto_train.py）
- `train_ability_nn.py` — DeepSets + champion ability-derived 靜態特徵，比較 LR / embedding-only / +ability
- `train_ability_tree.py` — LightGBM/XGBoost + ability 特徵，對比 champion-presence LR baseline
- `train_composition_lr.py` — champion 身分 + team-composition 特徵的 LR baseline（可 promote 版本）
- `train_composition_lr_pooled.py` — 跨 patch pooled + recency-weighted composition LR candidate
- `train_deepsets_pooled.py` — DeepSets 變體 vs pooled LR 同 split 對比（flat / recency / pretrain→finetune，可 multi-seed）
- `train_score_nn.py` — DeepSets + 英雄 composition score（wave_clear/CC/engage/damage/poke/sustain/frontline）
- `train_semantic_tree.py` — 樹模型 + team-level 語意 composition 特徵（測試「隊伍需要均衡混搭」假說）
- `train_single_team.py` — 單隊預測 P(win)（未知對手），LR vs DeepSetsSolo
- `train_tier2.py` — DeepSets + patch embedding 跨 patch 訓練（Tier 2）
- `auto_train.py` — 每 30 分鐘輪跑 variant 的自動訓練 runner（round-based frozen snapshot）

## 4. Ablation（一次性實驗，凍結）
- `ablation_additive_vs_nonadditive.py` — composition 特徵拆可加/非加性，驗證是否與 champion one-hot 冗餘
- `ablation_champ_archetype_persistence.py` — Q3：champion × team-archetype 交互能否 out-of-sample 重現
- `ablation_champ_role_persistence.py` — champion × teammate-role 交互（pooling 版本，取代 raw pair synergy）
- `ablation_composition_subset.py` — composition 特徵在哪個傷害組成區間才有價值
- `ablation_cross_patch_backbone.py` — 新 patch 資料稀缺時，跨版本 kit backbone 是否有幫助
- `ablation_pair_synergy_persistence.py` — 英雄×英雄 pair synergy 能否撐過 train→test（winner's curse 檢定）
- `ablation_recency_weight.py` — 跨 patch 訓練的時間衰減（recency）權重，掃 tau 找最佳半衰期
- `ablation_recommender_backtest.py` — 推薦器 backtest：paired CI + 隱藏一位英雄排名驗證（Part A/B，`--kept-extreme` 尾巴隊）
- `ablation_team_archetype_clusters.py` — Q2：資料驅動 team-archetype 分群 vs 6 個手調 archetype

## 5. 分析（一次性，凍結）
- `analyze_behavior_channels.py` — 找最佳「免費 elo-proxy」玩家行為訊號（counter-item 採用率、build entropy 等）
- `analyze_champ_by_lobby_skill.py` — 英雄勝率是否因 lobby 技術水準分層而不同
- `analyze_composition_signals.py` — champion 強度控制後的 team-composition 訊號分析
- `analyze_lobby_skill.py` — Mayhem 是否存在「行為向 elo」分層（stability/assortativity/gradient 三檢定）
- `analyze_meta_axis.py` — 行為 lobby tier 軸是否存在（不靠 win/loss）
- `analyze_performance_skill.py` — champ-controlled 表現能否當作可恢復的 ARAM 技術訊號
- `analyze_rank_behavior.py` — 不同 SR 段位玩家的 build / champ 偏好是否不同
- `analyze_rank_performance.py` — champ-controlled 表現是否隨 SR 段位提升（技術 vs 玩法風格）
- `analyze_role_augments.py` — 依 patch 分析各職業勝率與 augment 勝率
- `analyze_role_spells.py` — 各職業選用召喚師技能的勝率（相關非因果）
- `anchor_synergy.py` — anchor-conditional synergy：某英雄在隊上時，隊友如何位移隊伍勝率
- `synergy_lift.py` — 統計版 synergy lift 排名（候選英雄搭配你現有 4 人隊的加成）
- `synergy_lift_nn.py` — NN 版 synergy lift，比較是否與統計版排序不同
- `pairwise_and_stack_solo.py` — 單隊實驗：pairwise LR 特徵 + LR-residual NN stacking 比較
- `compare_combined.py` — 同 val/test 下比較單 patch 訓練 vs 跨 patch合併訓練
- `build_poke_review_html.py` — poke score 分桶校準用的可拖曳 HTML review board
- `build_wave_review_html.py` — wave-clear score 分桶校準用的可拖曳 HTML review board
- `build_semantic_score_review_page.py` — 語意 engage/wave 排名的靜態 review 頁（可在瀏覽器手動調序）
- `build_team_archetype_review.py` — Q2 team-archetype 分群結果 render 成 review HTML
- `build_skill_scaling_rating.py` — 每英雄「skill-scaling」評分（高技術 lobby 勝率 − 低技術 lobby 勝率）
- `build_wr_display_demo.py` — 勝率顯示 demo（raw WR / empirical-Bayes 估計 / Wilson lower bound 信心分數）
- `build_wr_site_preview.py` — 上述 WR 顯示方案的站台風格預覽

## 6. 英雄評分 / radar / skill 特徵
- `build_champion_radar.py` — 產生 `docs/champion-radar.html`，職業內百分位 6 軸雷達 + 玩家 OVR 模式
- `build_champ_archetype_fit.py` — 產生「英雄 × team-archetype」comp-fit 正式 artifact（Q3 訊號 productionize）
- `build_champ_empirical_axes.py` — 每英雄實測 scaling/snowball 軸（取代舊 engage/poke bars）
- `build_empirical_champion_scores.py` — 用 LCU 實測 participant 數據覆寫 damage/cc/frontline/sustain score
- `build_semantic_ability_scores.py` — 從技能文字 heuristic 產生 0..3 語意能力分數
- `build_skill_semantic_features.py` — 建立可 review 的 skill-level 語意特徵表（Data Dragon → semantic 中間層）
- `fetch_champion_abilities.py` — 從 Data Dragon 抓英雄 Q/W/E/R 技能原始資料
- `build_aram_skill.py` — 用 champ-controlled 表現校準每位玩家的 ARAM-skill 分數（對照 SR rank）
- `build_player_ratings.py` — 每玩家 Glicko rating + 每局品質分數（本機專用，含 puuid）
- `build_game_skill_by_id.py` — 用 game_id+puuid 產生可 join 訓練資料集的 lobby-skill 表

## 7. Pair / synergy / composition 模型工具（餵給 recommender pipeline）
- `build_pair_stats.py` — 產生 anchor-conditional 英雄 pair synergy JSON（`models/.../pair_synergy_*.json`，legacy）
- `build_role_synergy.py` — 產生 anchor-conditional 英雄×職業 synergy JSON（取代 raw pair synergy，r 0.17→0.37）
- `build_pooled_champ_lr.py` — 產生生產用 pooled + recency-weighted champion-only LR（`composition_lr_pooled_recency_7d`）
- `build_single_team_calibration.py` — 重新校準單隊顯示勝率的 intercept（修正雙隊 bias 讀數膨脹）
- `export_lr_weights.py` — 把訓練好的 LR pickle 抽成 JSON（更小、免 sklearn 依賴）

## 8. Recommender（champ-select 推薦）
- `recommend_gui.py` — Tk GUI champ-select 推薦器（桌面捷徑「ARAM Recommender (source)」pin 此路徑，永遠跑 source）
- `pick_advisor.py` — 指定 4 人隊，對候選第 5 位算單體強度 + 隊友 synergy 排名
- `refresh_recommender_models.py` — watchdog 用的 model 自動 refresh CLI（wraps `aram_nn.site.model_refresh_cli`）
- `build_recommender_exe.py` — 打包 PyInstaller Windows exe（**已棄用路徑**，recommender 現在永遠跑 source，不要重建）

## 9. Rank / skill 解析（外部 SR rank 橋接）
- `extract_participants.py` — 從 LCU DB 抽全量 participant 表（items/augments/spells/riotId）
- `extract_performance.py` — 抽每局 box-score（KDA/傷害/補刀等,供 skill 分析）
- `resolve_player_ranks.py` — riotId → account-v1 → league-v4 橋接解析真實 SR 段位（單 key，需 `RIOT_API_KEY`）
- `resolve_ranks_parallel.py` — 多 Riot key 平行解析版（N key → N thread）
- `resolve_ranks_rotate.py` — 單執行緒、key 輪替解析版（避免 429，吞吐量接近但更穩）

## 10. 資料工具 / 診斷
- `export_lcu_parquet_stream.py` — 串流把 LCU games SQLite 轉成訓練用 parquet schema
- `export_pooled_parquet.py` — 匯出跨 patch pooled Mayhem parquet（供跨 patch 訓練）
- `check_db.py` — 印 games.db 總場次/Mayhem 場次/待爬佇列長度（一行診斷）
- `probe_queues.py` — 探測目標段位玩家實際在打的 queueId
- `probe_user.py` — 探測特定玩家最近對戰,確認 Mayhem/Brawl 真實 queueId
- `sanity_tests.py` — NN pipeline 健檢（反對稱性、排列不變性、label-shuffle 洩漏檢測等）
- `smoke_test.py` — 抓 1 位高段玩家 + 1 場對局,驗證 Riot API 抓取管線基本可用

## 11. Scratch（gitignored，勿在新程式碼引用）
一次性 scratch 分析,已 `.gitignore`（`_*.py`）,結果不保證可重跑、不保證維護：
`_dropbear_lift.py`、`_games_count_control.py`、`_omnisoul_slot_wr.py`、`_randuin_analysis.py`、`_randuin_champ_control.py`、`_randuin_crit3_tank_hs.py`、`_randuin_vs_tanks_by_crit.py`
