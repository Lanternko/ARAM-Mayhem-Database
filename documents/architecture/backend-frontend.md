# Backend / Frontend Architecture

## Current production shape

公開站目前是 split static site：GitHub Pages 從 `main` branch 的 `/docs` 提供 HTML/CSS/JS shell，瀏覽器再讀取 `docs/api/tier-list.json` 與 per-champion JSON。`docs/` 是 build output，不是手寫文件來源。

網站 source of truth 是 `scripts/templates/`、renderer/builder 與 `src/aram_nn/site/static_publish.py`。正式發布流程見 `../../runbooks/site-deploy.md`。

## Private collector boundary

Mayhem 資料由本機 League client 的 LCU collector 取得。以下資料只留在本機：

- `crawl_seen`、`crawl_queue` 與 seed/frontier state。
- `riot_id_bridge`、PUUID、summoner name、Riot ID。
- 多 client 各自的完整 local DB 與 operational logs。

Exact match upsert key 是 `game_id`。多 client 只能各寫各的 DB，再用明確 merge 流程合併 `games`；不可共寫同一個 frontier。

## Public static payload

Publisher 從 private source 建立只含網站所需統計的公開 JSON。生成 artifact allowlist 只由 `DEFAULT_DOC_PATHS` 管理，避免 runbook 或 agent context 各自維護不同清單。

Data-only publisher 在 disposable isolated worktree build、commit、push，不要求 development worktree clean，也不會把其他 WIP 帶入 publish。

## Optional games backend

`src/aram_nn/site/` 與 `scripts/site_api.py` 提供 optional FastAPI backend。它不是 GitHub Pages 顯示 tier list 的必要 runtime；用途是接收公開 game rows、提供 tier/augment API，以及 Meta Pick server-side scoring/leaderboard。

主要 contract：

- `POST /games/bulk` 依 `game_id` idempotent upsert。
- Tier、champion augment 與 health endpoints 讀公開 schema。
- Meta Pick run 固定由 server 重新計分，不信任 client 傳入的 rank/score。
- CORS 只有明確設定 origins 時啟用。
- Rate limit 是 best-effort in-process control，不等於跨 worker 的 durable quota。

## Privacy contract

公開 games schema 僅保留：

- `game_id`、queue、patch 與時間欄位。
- 兩隊排序後的 champion IDs 與藍方 win flag。
- 網站統計需要的 duration 與 public participant stats。

Bulk ingest 拒絕含 `puuid`、`summonerName`、`riotId` 等 private field，或看似 UUID/PUUID 的 participant payload。任何 schema 擴充都要先證明是產品所需且不會重新識別玩家。

## Sync and publication ownership

`scripts/sync_site_backend.py --watch` 是 local DB → optional public backend 的獨立鏈，不由 crawler watchdog 啟動。它依 filtered local games 相對上次成功 upload 的成長 watermark 決定推送；首次灌資料才用 force 類操作。

GitHub Pages data publication 則由 `scripts/publish_static_site.py` 擁有，現行 production policy 是 10% growth 與最長 12 小時門檻。兩條鏈不可混稱為同一個 deploy。

## Change routing

- 網頁美學、tokens、頁面與 responsive：`../../DESIGN.md`。
- Static build/publish：`../../runbooks/site-deploy.md`。
- Live crawler/backend topology：`../../OPERATIONS.md`。
- CLI 入口：`../../scripts/README.md` 與各命令 `--help`。
