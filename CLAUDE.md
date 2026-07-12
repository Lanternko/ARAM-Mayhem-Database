<!-- lines: 142 -->
# aram-winrate-nn — ARAM 英雄組合勝率預測 NN，Python / PyTorch

## Why
輸入一場 League of Legends Mayhem (queueId=2400) 或 ARAM (450) 的雙方英雄組合 (5v5)，輸出藍方獲勝機率。
目標是驗證「提供英雄組合資訊後，模型準確率能否超過藍方 base rate (~51%)」— 已達成。

**主資料源是 Mayhem (queueId=2400)**，30k+ 場；ARAM (450) 因 Riot 公開 API 不限制曾收 ~7k 場但已棄。
Repo 名留 `aram-winrate-nn` 是歷史包袱，**所有訓練 / tier list / 推薦都該預設 Mayhem**。`train.py` / `train_tier2.py` / `tier_list.py` queue-agnostic — 吃任何符合 schema 的 parquet，不檢查 queue。

## Architecture
- **Python 3.13**, PyTorch 2.11, polars, scikit-learn, httpx, psutil, click
- `src/aram_nn/ingest/` — Riot API 爬蟲：`riot_client.py` / `snowball.py` / `extract.py`
- `src/aram_nn/lcu/` — 本機 LCU collector / graph snowball：`process.py` / `client.py` / `poller.py` / `snowball.py`
- `src/aram_nn/models/` — `logreg.py` / `deepsets.py`
- `src/aram_nn/gamedata.py` — games.db 共用 loader（`iter_games` / `load_games_df` / `count_games`，read-only 連線），編碼 queue/patch 過濾、champs 升序、participants 解析等慣例；**新分析腳本從這裡 import，別再手刻 sqlite loader**。帶 participants 預設無序循序掃（~70s/patch）；`ordered=True` 是 seek-bound two-pass，只在真要時間序時用
- `src/aram_nn/train.py` / `eval.py` / `data.py` — 訓練 pipeline（完成）
- `data/raw/` — parquet 原始資料；`data/lcu/games.db` — LCU SQLite 資料庫（`games` + `crawl_seen` set + `crawl_queue` priority frontier）
- `scripts/` — `probe_user.py`, `probe_queues.py`, `lcu_collector.py`；tier-list builder 拆 3 檔：`build_tier_list.py`（CLI 入口 ~394 行 + `--shell-only`）+ `tierlist_engine.py`（勝率/augment/affinity/cluster 計算引擎 ~4.2k 行）+ `tierlist_render.py`（render_html / shell-only / payload dedup / OG·favicon 圖 ~1.5k 行）。`build_tier_list` 用 `globals().update(vars(tierlist_engine/render))` re-export 全部符號（含底線），所以舊的 `import build_tier_list` 完全不受影響。改計算改 engine、改前端輸出改 render
- `scripts/templates/site.css` + `site.js` — 站台 CSS/JS 模板（`_read_site_template`）。**build 必需，勿 `git clean`**；改前端編這兩檔，不是改產物。CSS 仍 inline 進 HTML；JS 在 split build 輸出成 `docs/assets/site.js`（runtime 以 `location.origin` 解析 + content-hash `?v=`，15 份 SPA shell 共用一份瀏覽器快取），deploy 必須跟 index.html 同 commit
- `docs/index.html` + `docs/api/tier-list.json` — 公開 tier-list 網站（GitHub Pages, `main` branch `/docs` folder）→ https://arammeta.com/。payload 的 item 物件已 dedup 成 top-level `itemLut`+`ddv`（嵌入處只留 `{id, ic:1}`+stats，前端 `rehydrateItems()` 還原 name/icon）；server `data-search` 只含英雄名，augment/item 詞由前端 `enrichSearchIndexes()` 從 payload 重建
- `src/aram_nn/site/` — 前後端分離層：公開 `games` DB schema、FastAPI backend、10k watermark sync、tier-list JSON payload API
- `data/cache/` — `kiwi.bin.json` + `lol_stringtable_zh_tw.json` (CommunityDragon mirror, ~30 MB) 用來解析 Mayhem augment 中文敘述
- 深度技術決策見 `PLAN.md`（v3，已經 Codex review）；部署流程見 `.claude/skills/deploy-tier-list/SKILL.md`

## Commands
> 範例使用 bash `\` 換行。**PowerShell 改用 backtick `` ` ``（行尾不能有空白）**，或整段貼成一行 — `\` 在 PS 不是換行符會直接報 `unexpected extra argument`。

```bash
# 安裝（editable，每次加新 entry point 後要重跑）
python -m pip install -e .

# 資料抓取（Riot 公開 API 的 ARAM 路徑，已少用）：--patch 16.9 過濾版本，省略 --patch = 全收
python -m aram_nn.ingest.snowball --region tw --seed-riot-id "Name#TAG" \
    --target-matches 2000 --patch 16.9 --out data/raw/tw_aram_16_9.parquet

# 診斷：查某 Riot ID 最近打了哪些 queue
python scripts/probe_user.py --region tw --riot-id "Name#TAG" --count 100

# Tier list 網站 split build（部署 → `/deploy-tier-list` skill）
python scripts/build_tier_list.py --site-url "https://arammeta.com/" --payload-out docs/api/tier-list.json --payload-url api/tier-list.json

# 改前端後「快速預覽」：重用現有 tier-list.json、跳過所有勝率/affinity 計算（~1.4s vs full build ~17min）
# 只重生 index.html；改 site.css / site.js / 文案 / 專欄後用這個看效果，要刷新資料才跑上面的 full build
python scripts/build_tier_list.py --shell-only --out docs/index.html

# 舊 inline build 仍可用於臨時單檔測試；正式 deploy 不用這個模式
python scripts/build_tier_list.py --site-url "https://arammeta.com/"

# VM/backend API + local 10k watermark upload
python scripts/site_api.py
python scripts/sync_site_backend.py --api-url http://127.0.0.1:8000 --watch
```

## LCU Collector (Mayhem data, local client only)

Riot 在 API 層級封鎖 queueId=2400，只能走本機 League client 的 LCU API（見 Riot API 節）。

```powershell
python scripts/lcu_collector.py collect --queue 2400   # 開 League 前先跑，收 Mayhem
python scripts/lcu_collector.py status
python scripts/lcu_collector.py snowball-workers --workers 3 --seed-riot-id-file data/seeds/opgg_tw.txt --target-games 5000 --max-players 5000 --games-per-player 4
python scripts/lcu_collector.py export --queue 2400 --out data/raw/mayhem_games.parquet
```

完整子指令目錄（seed-opgg-plan / metrics / family-stats / merge-db / dataset / stats…）→ `OPERATIONS.md` §6；**Stall Playbook**（seed family 判斷、backoff 陷阱、LCU 憑證假過期）→ `OPERATIONS.md` §7。

- LCU 只保留**最近 ~20 場**；每次遊戲 session 都要跑 collector，否則漏場。
- OPGG seeding（`seed-opgg-plan` → `--seed-riot-id-file`）是 TW Mayhem 預設 seed strategy；`ladder` / `apex` / `riot-tier` 是已驗證 dead seed family，只在換 region 或大版本後重驗 ROI。
- `crawl_seen` + `crawl_queue` 讓 snowball 可中斷續跑；`data/lcu/games.db` (SQLite) 隨時可安全中斷。
- 多 client：各寫各的 `--db`；`merge-db` 只合併 `games` 表並以 `game_id` 去重，`crawl_seen` / `crawl_queue` frontier 不要跨 client 合併。

## Backend / Frontend Split

- GitHub Pages 目前是 static split：`docs/index.html` 是小型前端殼，載入 `docs/api/tier-list.json`；不是 live backend API。
- 本機 collector DB 仍是 private source of truth；`crawl_seen` / `crawl_queue` / `riot_id_bridge` 不離開本機。
- Website backend 用 `src/aram_nn/site/db.py` 的公開 `games` schema，`POST /games/bulk` 以 `game_id` idempotent upsert，會拒絕含 `puuid` / `summonerName` / `riotId` 或 UUID-like PUUID 的 `participants_json`。
- `scripts/sync_site_backend.py --watch` 預設每新增 10,000 筆 filtered local games 才推一次；可用 `--force` 首次灌資料。
- `scripts/build_tier_list.py --payload-out ... --payload-url ...` 會把前端資料切到 JSON；省略 `--payload-url` 就保留舊版 inline HTML。

## NEVER

- **Never filter queue inside `extract_row`** — 它只驗結構（10 人、雙方各 5 人、有 win flag）；queue 過濾由 caller (snowball.py) 負責。違反此原則曾導致全部場次被誤判為 parse error。
- **Never sort champions by position / slot index** — 隊內位置在 ARAM 無意義；`blue_champions` / `red_champions` 永遠以 `championId` 升序排列，否則 model 會學到 position spurious feature。
- **Never use random train/val/test split** — 用時間切分（`game_creation_ms`）；隨機切會讓同 meta 的場洩漏進 val/test。
- **Never add label smoothing** — calibration 用 post-hoc temperature scaling on val set；pre-hoc smoothing 讓 ECE 難以解讀。
- **Never train on cross-patch data without patch feature** — 不同 patch 的英雄平衡差異大；若跨 patch 訓練，至少要加 `patch` embedding 或 per-patch evaluation。
- **Never call `match_ids_by_puuid` without `queue=450`** during snowball — 不 filter queue 只用在 diagnostic scripts，大量 ingest 一定要加 queue filter 避免收 SR / Arena 場。
- **Never dedupe matches by champion-composition hash alone** — 不同真實對局可能剛好出現同一組 10 隻英雄；crawl / dataset exact dedupe 必須用 `game_id`，composition hash 只能當分析輔助欄位。
- **Never hardcode routing host** — TW 的 match-v5 走 `sea.api.riotgames.com`，account-v1 走 `asia.api.riotgames.com`，platform (league-exp) 走 `tw2.api.riotgames.com`；三個不同，搞混會 404。
- **Never pass `--patch ""`** in PowerShell to CLI — PowerShell 5.1 會把空字串吃掉導致 Click argument shift；省略 `--patch` 即為全收（預設值已是空字串）。
- **Never publish the tier-list site from `/site`** — GitHub Pages 「Deploy from a branch」只接受 `/(root)` 或 `/docs`；用 `/site` Save 不會生效。永遠輸出到 `docs/index.html`（`build_tier_list.py` 預設）。
- **Never `git add -A` / `git add .` when deploying the site** — 工作樹常有未追蹤的 WIP scripts；只 stage `docs/index.html`、`docs/assets/site.js`（外部化 app script，HTML 以 content-hash `?v=` 引用，漏 stage 會讓線上跑舊 JS）、`docs/api/tier-list.json`，以及本輪生成且確認需要的 `docs/champion-roles.json` / `docs/og-image.png`。不要 stage `scripts/build_tier_list.py`，除非 generator change 本身也要發布。
- **Never import a script from another script for shared code** — `from train_ability_nn import ...` 這類耦合已讓 3 支腳本被 30+ 支下游綁死、動一支壞一片。既有的凍結腳本不動；**新**共用函式一律進 `src/aram_nn/`（資料存取用現成的 `gamedata.py`），腳本只從 package import。

## Riot API 注意事項
- Dev Key 每 24 小時過期，Python 端 401 / 403 都視為 key expired → 提示 regenerate
- Rate limit: 20 req/sec, 100 req/2min（binding constraint）；`riot_client.py` 已內建 bucket throttle
- Mayhem (queueId=2400) 被 Riot **在 API 層級整場移除**，公開 dev key 完全拿不到，不要嘗試
- Mayhem 資料唯一合法管道：本機 LCU (`127.0.0.1:{port}`) + Live Client Data (`127.0.0.1:2999`)；見 LCU Collector 節

## Model 設計原則（快速參照，詳見 PLAN.md）
- Tier 0 (LR baseline) 必跑才能判斷 NN 是否有效
- Tier 1 輸入 `[diff=(sum_blue−sum_red), total=(sum_blue+sum_red)]`，不是只有 diff — 純 diff 會丟掉兩隊共有的上下文
- Logit 必須對 swap-teams 反對稱：`logit(blue, red) = −logit(red, blue)`
- acc > 65% = data leak，立刻檢查 split

## 專案佈局（2026-07-06 整理）
- 新分析輸出進 `outputs/<類別>/`（`ablation/` `figures/` `reviews/` `logs/`）、報告進 `documents/reports/`、匯出檔進 `documents/exports/`、共用程式進 `src/aram_nn/`；腳本不互相 import、根目錄不放新腳本 — why：`outputs/`/`documents/` 全 gitignore，分類只影響本機可讀性，不影響 git；`scripts/` 平鋪 99 個檔靠 `scripts/README.md` 索引導航，見該檔分類。
- Harness 排程鏈 / 發布鏈 / 同步鏈總圖見 `OPERATIONS.md`，不在此重複。

## How to edit this file
- Keep the whole file under 300 lines. If a new rule pushes past that, remove a stale one.
- Every rule states Why in the same line or the next.
- No style/formatting rules here — those live in the linter config.
- When a mistake repeats, abstract it into one concise rule and add it.
- After editing, update the top-of-file summary if sections changed.
