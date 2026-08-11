<!-- lines: 116 -->
# aram-winrate-nn — Mayhem 資料、勝率推估、推薦與公開產品，Python / PyTorch

## Why
這個專案起點是驗證「只知道雙方英雄組合，能否比藍方 base rate 更可靠地預測勝負」；現在已演化成 Mayhem（queue 2400）的資料收集、統計、Draft 推薦與公開網站系統。

核心問題不是預言單場輸贏，而是隔離玩家在開局前真正看得見、也能控制的變量：英雄與隊伍組合。產品應輸出可信的期望勝率、相對強弱與可解釋的改善方向，幫使用者比較選項；不可把高變異的競技遊戲包裝成確定答案。

Mayhem 是產品與研究預設。ARAM（450）是歷史路徑；2450／4310 目前只隨 LCU history 低成本收集。任何分析都必須明示 queue、patch、region、時間範圍與資料來源，不可因 schema 相同就混在一起。

## Architecture (What)
系統有四個平面，資料只能沿著可追蹤的方向往下游流動：

1. **Capture plane** — `src/aram_nn/lcu/`、`scripts/lcu_collector.py` 與 OPGG/manual seed graph 收本機 LCU 對局；Mayhem 不存在於 Riot 公開 API。
2. **Data plane** — `data/lcu/games.db` 是 private source of truth；所有 queue 共用 `games` 表，`crawl_seen`／`crawl_queue`／`riot_id_bridge` 管 persistent frontier。`src/aram_nn/gamedata.py` 是新分析的 canonical read-only loader。
3. **Evidence plane** — `src/aram_nn/models/` 與訓練 pipeline 做時間切分 benchmark；`scripts/tierlist_engine.py` 做勝率、Bayesian shrinkage、pair／augment／item 統計；已結束 patch 的 raw counters 才能進 snapshot。
4. **Product plane** — `scripts/tierlist_render.py`＋`scripts/templates/` 生成網站，`src/aram_nn/site/` 管 public schema、同步與 isolated publisher；recommender 與 Draft UI 消費同一套經驗證資料，而不是另造口徑。

`scripts/mayhem_lcu_watchdog.py` 是本機 runtime harness 的 parent，管理 crawler workers、static publisher 與 model refresh；這台機器的 production profile 與 task 拓撲以 `OPERATIONS.md` 為準。

## Decision Doctrine
- 先寫清楚**要做的決策**，再選指標。研究問題、資料刷新、模型選擇、UI 改版的成功條件不同，不可都用「數字變大」代替判斷。
- 把**可控因素**與**不可控噪音**分開。Composition 是 draft lever；玩家技術、熟練度、道具、增幅、配合與臨場事件是未觀測變量。模型估的是對這些噪音平均後的條件期望。
- 把產品視為**比較器，不是算命器**。優先判斷「A 相對 B 是否穩定更好、改善多少、在什麼 regime 有效」，而非追求每場猜中。
- 依證據層級做決策：held-out／walk-forward 結果高於 training fit；paired comparison 高於兩個獨立 headline；跨 patch／多 seed 穩定性高於單次最佳 run；可重現 artifact 高於 terminal 截圖。
- 區分相關與因果。網站勝率描述歷史關聯；只有明確 treatment、control 與干擾控制的實驗，才能使用因果語言。
- 新 default 必須回答三件事：相較哪個 baseline、在哪個資料範圍改善、代價或退化在哪裡。沒有這三項就維持現狀或標成探索性結果。
- 優先修正資料定義與評估設計，再增加模型複雜度。這個領域最危險的錯誤通常是 leakage、patch 混淆、樣本偏差或錯誤 baseline，不是網路不夠深。
- 決策必須可撤回：保留原始 counters、資料 lineage、舊 baseline 與 migration path；不要只保存經平滑或渲染後的成品。

## Data Quality Doctrine
高品質資料不是單純「場數多」，而是每筆資料的身份、範圍、來源與轉換都能回答：

- **Identity** — 一場真實對局只由 Riot `game_id` 識別與去重；英雄組合相同不代表同一場。
- **Structural validity** — 只接受 10 位參與者、雙方各 5 人、team/win flag 完整的 record；`extract_row` 只驗結構，queue filtering 留給 caller。
- **Scope** — dataset／報告／公開 payload 一律帶 queue、patch、region、time cutoff、row count 與主要 exclusion；跨 patch 不直接當同一環境。
- **Provenance** — 記錄資料來自 public API、LCU poller、snowball、匯入檔或 snapshot；不要以 `latest` 檔名取代可重現的 dataset identity。
- **Stable semantics** — 英雄集合按 `championId` 升序；4310 Jade ID 先正規化；稀疏 participant stat 缺鍵視 schema 定義處理，不用分表或任意補值改變意義。
- **Freshness and completeness** — LCU 只留最近約 20 場，漏收無法完整回補；新 patch 的低樣本、seed-family selection bias 與 late-arriving tail 都要在解讀中揭露。
- **Privacy** — PUUID、Riot ID、summoner name 與 private frontier 永不進 public payload；網站只發布去識別化聚合統計。
- **Statistical reliability** — 小樣本不可直接按 raw win rate 排名。公開數字使用 sample floor、Bayesian/empirical-Bayes shrinkage、confidence-aware ordering，並同時呈現樣本數與版本。

細部 dataset contract、實驗 manifest 與驗收流程見 `notes/agents/experiments-and-data.md`。

## Experiment Doctrine
- 每次實驗先固定：decision、hypothesis、experimental unit、treatment、control、資料範圍、split、primary metric、guardrail 與 promotion rule；結果出來後不可改題目配答案。
- 一次只改一個主要變量。若資料、特徵、split、模型與超參同時變動，結果只能算探索，不能歸因；需要多變量時做明確 factorial／ablation。
- 同一比較使用相同 rows、相同 chronological split、相同 preprocessing 與相同 label definition。能做 paired delta 就不要只比兩個各自的平均。
- Test set 只做最後稽核，不拿來選 feature、threshold、seed 或文案。調參用 train／val；需要反覆決策時使用 expanding-window 或新的時間 holdout。
- Baseline ladder 不可跳級：constant blue-base-rate → champion-identity LR → composition LR → DeepSets／更複雜模型。複雜模型必須證明超過它實際新增資訊的最近 baseline。
- Win-rate 產品以 log loss、Brier／ECE、calibration bucket 與 decision uplift 為主；accuracy 是輔助 sanity check。`>65%` 先停下查 leakage，不視為突破。
- 報告 effect size、樣本數、uncertainty 與 failure regime，不只報最佳值。NN 要報多 seed variance；相同 matches 上的模型差異優先用 paired bootstrap／paired test。
- 結果 promotion 前必須通過資料不變量、相關單元測試、out-of-sample evidence、產品 guardrail 與可重現 artifact；負結果也保存，避免之後重跑已證偽方向。

## Product and Web Design Doctrine
- 網站是**可信的資料工具**，不是炫技 landing page。視覺方向是 engineered calm：近黑冷灰底、低彩度 surface、克制金色 accent、細邊線與少量高光，讓數字與決策優先。
- 資訊層級固定為：**先給答案／建議 → 再給比較依據 → 最後給樣本、版本與限制**。重要勝率、lift 與選項要能掃讀；metadata 應安靜但不可被藏掉。
- 保持高資訊密度，但避免 card-in-card。大型面板偏好單一平面＋hairline section；留白用來分組，不用更多陰影、漸層與彩色框製造層級。
- 色彩必須有語義且節制：金色代表品牌／主要互動，正負勝率色只標關鍵差異，角色／tier／rarity 色限定在對應 encoding；不可把每列做成 traffic light。
- Typography 以 `Noto Sans TC`／系統 sans 支撐密集繁中 UI，數字使用 tabular figures；serif 只在少數 editorial／metadata caption 使用，不當主要介面字體。
- Dark／light theme 必須走 token，而不是元件散落硬編色；新元件兩個 theme 同時完成。動效只解釋狀態與空間關係，遵守 duration/easing scale 與 `prefers-reduced-motion`。
- Desktop、mobile、鍵盤與 touch 是同一產品，不是縮小版補丁。互動元件必須有語義 HTML、focus state、ARIA 狀態與可達的 mobile layout。
- 效能屬於設計品質：首屏 payload 保持精簡，champion detail 按需 shard，shared JS 可 cache 並以 content hash bust；UI-only 改動不可強迫重跑昂貴統計。
- 繁中是 canonical product voice，簡中與英文必須保持相同資訊拓撲與決策含義，不可只翻表面 label。
- UI source of truth 是 `scripts/templates/site.css`、`site.js` 與 renderer；`docs/` 是生成產物。完整品牌字標、頁面架構、CSS 美學、token 與驗證規範見根目錄 `DESIGN.md`。

## Operations and Change Control
- 先辨識 canonical source、資料 owner 與正在跑的 process，再修改；生成檔、live SQLite、ignored local service assets 與使用者 WIP 有不同風險邊界。
- 一個 task 使用一個 `codex/<task>` worktree；mutation 前先查 status 與現有 worktrees，因為 primary checkout 是 live harness anchor，dirty／stale worktree 不可被自動重用或清理。
- 研究輸出進 `outputs/<category>/`，長期報告進 `documents/reports/`，共用程式進 `src/aram_nn/`；不要讓 scratch script 變成隱性 production dependency。
- Site publish 分 routine data、frontend shell、generator／schema 三條 lane；明確的 deploy／publish／ship／上線要求已授權相符的 build、commit、push，不再二次確認。GitHub Pages 是靜態發布，不會重啟 crawler、backend 或其他 runtime。
- 變更驗證按風險分層：純文件做連結／結構檢查；資料與模型做 invariant＋held-out evidence；UI 做 code test＋雙 theme／多 viewport visual QA；live harness 變更再核對 task argv、state 與 recovery log。
- Runtime tuning 以 wrapper 的 production argv 為準，不以 Python generic defaults 或舊文件猜測。任何自動 recovery 都要留下 action、threshold、LCU status 與當時 memory，才能讓下一次決策基於證據。

## Commands (How)
根文件刻意不保存會過時的長指令。執行時依任務進第二層 source of truth：

- 安裝、測試、訓練、分析與腳本分類：`scripts/README.md`、`pyproject.toml`、目標 CLI 的 `--help`。
- 本機排程、process、state、log、觀測與重建：`OPERATIONS.md`。
- Crawler stall／LCU recovery：`runbooks/crawler-stall.md`。
- OPGG seed refresh：`runbooks/opgg-seed-refresh.md`。
- GitHub Pages routine data／frontend shell／generator-schema publish：`runbooks/site-deploy.md`。
- Git／worktree 建立、更新、整合、push 與清理：`runbooks/git-workflow.md`。
- 外部資料貢獻與 share 驗證：`CONTRIBUTING.md`。
- 實驗執行與 dataset manifest：`notes/agents/experiments-and-data.md`。
- UI preview、CSS 修改與 visual QA：`DESIGN.md`。

## NEVER
- Never use random train／val／test split or tune on test；時間與 meta leakage 會讓所有 model comparison 失效。
- Never train cross-patch without patch feature or per-patch evaluation；不同平衡環境不能當 IID 樣本。
- Never use post-game outcome proxies as pre-game features；產品只能使用決策當下可取得的資訊。
- Never dedupe exact matches by champion composition；exact identity 一律是 `game_id`。
- Never sort champions by slot／position；隊內順序在 ARAM／Mayhem 無意義，會製造 spurious feature。
- Never split `games.db` by queue or move its DB／WAL／SHM while collector runs；全 repo 與 live SQLite 都依賴單表、單一三件套。
- Never publish player identifiers or private frontier；公開資料只允許去識別聚合。
- Never rank low-sample cells by raw win rate without uncertainty control；這會把 sampling noise 包裝成推薦。
- Never settle the current patch or freeze derived win rates；只保存已結束 patch 的 raw counters，讓方法可重算。
- Never edit generated `docs/` as source or publish a partial generated set；改 template／renderer，deploy 使用 atomic allowlist。
- Never put new shared logic in another script；共用 code 進 `src/aram_nn/`，避免 script-to-script dependency 擴散。
- Never pull／rebase／merge／switch／remove a dirty worktree automatically；先盤點 tracked、staged 與 untracked ownership，避免覆蓋使用者 WIP。
- Never use `git add .`／`git add -A` or force-push；只 stage 已 review 的明確 path，已發布 history 以 merge 保留，因為工作樹常含 unrelated WIP。

## Scoped Rules
- `notes/agents/experiments-and-data.md` — dataset contract、變因控制、統計與 promotion checklist。
- `MODEL.md` — 現行研究問題、資料 contract、baseline ladder、模型不變量與 promotion gate。
- `DESIGN.md` — 品牌字標、product hierarchy、頁數、顯示模式、CSS token、美學、responsive、a11y、performance 與 visual QA。
- `OPERATIONS.md` — live harness 拓撲、production profile、狀態、log 與 recovery routing。
- `PLAN.md`、`notes/archive/` — 歷史決策與實驗快照；不可覆蓋 current owner。
- `scripts/README.md` — command／script index；`runbooks/` — agent-neutral 高風險操作 SOP。

## How to edit this file
- Keep the whole file under 300 lines. If a new rule pushes past that, remove a stale one.
- Every rule states Why in the same line or the next.
- No style/formatting rules here — those live in the linter config.
- When a mistake repeats, abstract it into one concise rule and add it.
- After editing, update the top-of-file summary if sections changed.
