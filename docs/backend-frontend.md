# Backend / Frontend Split

This repo now supports two deployment shapes:

1. **Legacy static build**: `scripts/build_tier_list.py` still reads a SQLite DB and emits `docs/index.html`.
2. **Split pipeline**: local collectors push public game rows to a backend API; the site can fetch a JSON payload instead of embedding all data in HTML.

## Local Windows Collector

Private crawl state stays local:

```powershell
python scripts/lcu_collector.py snowball-workers --workers 2 --seed-riot-id-file data/seeds/opgg_tw.txt --target-games 5000
python scripts/sync_site_backend.py --api-url http://127.0.0.1:8000 --watch
```

`sync_site_backend.py` only sends the public `games` columns. It skips `crawl_seen`, `crawl_queue`, `riot_id_bridge`, PUUIDs, summoner names, and Riot IDs. The default threshold is 10,000 filtered local games since the last successful upload.

## Backend API

Run locally:

```powershell
$env:ARAM_SITE_DB = "data/site/games_public.db"
$env:ARAM_SITE_ADMIN_TOKEN = "change-me"
python scripts/site_api.py
```

Endpoints:

- `POST /games/bulk` or `/api/games/bulk`: idempotent ingest by `game_id`.
- `GET /tier-list?patch=16.10` or `/api/tier-list?...`: API-friendly tier list JSON.
- `GET /champion/{id}/augments?patch=16.10` or `/api/champion/{id}/augments?...`: champion augment fit JSON.
- `GET /health` or `/api/health`: DB path and row count.
- `POST /meta-pick/runs` or `/api/meta-pick/runs`: submit a 5-round Meta Pick run (server re-scores).
- `GET /meta-pick/leaderboard?patch=&limit=` or `/api/meta-pick/leaderboard?...`: global board.

For installed editable entry points:

```powershell
aram-site-api --host 0.0.0.0 --port 8000
aram-site-sync --api-url https://your-vm.example --watch
```

## Meta Pick global leaderboard

A **run** is exactly 5 completed rounds (10 pool → pick 5). Client shows ranks for UX, but the API **never trusts client ranks/scores**: it reloads the tier-list JSON snapshot and re-scores every `C(10,5)=252` combo per round.

### Env vars

| Variable | Purpose |
| --- | --- |
| `ARAM_SITE_DB` | SQLite path (shared with games bulk ingest). Stores `meta_pick_runs`. |
| `ARAM_META_PICK_SNAPSHOT` | Path to tier-list JSON snapshot used for scoring (default: `docs/api/tier-list.json` if present). Must include `patch_prefix` + `champs` + `recommendation_composition`. |
| `ARAM_SITE_CORS_ORIGINS` | Comma-separated allowed origins. CORS middleware is **only** installed when set. Production example: `https://arammeta.com` (not hard-coded as the only origin). Allows `GET`/`POST`/`OPTIONS` and `Content-Type`. |
| `ARAM_META_PICK_RATE_LIMIT_PER_HOUR` | Best-effort **in-process** submit limit per client key (default `30`; `0` disables). Keyed by `Request.client.host`; trusts `X-Forwarded-For` only when `ARAM_META_PICK_TRUST_PROXY` is truthy. Does not persist raw IPs. **Single-process only** — each uvicorn worker has its own counter. |
| `ARAM_META_PICK_TRUST_PROXY` | When `1`/`true`, rate-limit key uses first `X-Forwarded-For` hop. |
| `ARAM_META_PICK_API_URL` | Build-time site inject (`--meta-pick-api-url`). Empty → frontend disables remote submit/board. |

### Snapshot rebuild / deploy

1. Full tier-list build writes `patch_prefix` into `docs/api/tier-list.json` (snapshot id).
2. Point the API at that file via `ARAM_META_PICK_SNAPSHOT` (or place it at the default path).
3. Build the static site with the API base, e.g.
   `--meta-pick-api-url "https://your-api.example"` (or env `ARAM_META_PICK_API_URL`).
4. After every data rebuild, **redeploy both** the JSON snapshot the API reads and the GitHub Pages shell so client `DATA.patch_prefix` matches the server snapshot. A stale client gets **HTTP 409**.

### Request / response example

```http
POST /api/meta-pick/runs
Content-Type: application/json

{
  "nickname": "路燈",
  "main_id": "67",
  "patch": "16.10",
  "rounds": [
    {
      "pool_ids": ["1","2","3","4","5","6","7","8","9","10"],
      "picked_ids": ["6","7","8","9","10"]
    },
    { "pool_ids": ["…10 ids…"], "picked_ids": ["…5 ids…"] },
    { "pool_ids": ["…"], "picked_ids": ["…"] },
    { "pool_ids": ["…"], "picked_ids": ["…"] },
    { "pool_ids": ["…"], "picked_ids": ["…"] }
  ]
}
```

```json
{
  "ok": true,
  "updated": true,
  "avg_rank": 12.4,
  "ranks": [8, 15, 10, 20, 9],
  "total_combos": 252,
  "patch": "16.10",
  "entry": {
    "id": 1,
    "nickname": "路燈",
    "main_id": "67",
    "patch": "16.10",
    "avg_rank": 12.4,
    "ranks": [8, 15, 10, 20, 9],
    "total_combos": 252,
    "created_at": "2026-07-13T12:00:00.123456Z"
  },
  "retained": null
}
```

Validation: unique ids, exact sizes (10/5), picked ⊆ pool, known champions, nickname 2–16 Unicode code points after trim/collapse, internal key = NFKC + casefold (not exposed in public entry dicts). Optional `main_id`: empty or a champion id present in the snapshot (used as leaderboard avatar). Legacy `flag` in the request body is ignored. **Same nickname may hold many board rows** (one per distinct 5-round run). Uniqueness is **run fingerprint × `patch`** only: SHA-256 of canonical pool+picks — re-uploading the same replay (any nickname) returns HTTP 409. Leaderboard sort: `avg_rank ASC`, then `created_at DESC`, then `id DESC`. List `limit` 1..100 (default 50). Rate limit per client IP applies to submit.

```http
GET /api/meta-pick/leaderboard?patch=16.10&limit=50
```

Default `patch` = snapshot `patch_prefix`. `limit` 1..100.

```json
{
  "patch": "16.10",
  "total": 42,
  "entries": [ { "nickname": "路燈", "avg_rank": 12.4, "ranks": [8, 15, 10, 20, 9], "…": "…" } ]
}
```

## Split Static Site Build

Generate HTML plus an external data payload:

```powershell
python scripts/build_tier_list.py `
  --site-url "https://arammeta.com/" `
  --payload-out docs/api/tier-list.json `
  --payload-url api/tier-list.json `
  --meta-pick-api-url "https://your-api.example"
```

The generated `docs/index.html` keeps the existing UI but loads `DATA` from `docs/api/tier-list.json`. Omitting `--payload-url` preserves the old fully-inline HTML behavior.

`--meta-pick-api-url` / `ARAM_META_PICK_API_URL` is injected into `site.js` as `__META_PICK_API_BASE__` (also works with `--shell-only`). Empty means the Meta Pick UI still works offline for the 5-round game, but submit/leaderboard stay disabled.

## Privacy Contract

The backend database is intentionally narrower than the collector DB. It stores only:

- `game_id`, `queue_id`, `patch`
- sorted champion IDs by side
- blue-side win flag, duration, creation time
- captured timestamp
- public participant stats needed for augments/items

Bulk ingest rejects `participants_json` if it contains PUUID-like UUIDs or private field names such as `puuid`, `summonerName`, or `riotId`.

## Isolated public player-history service

Player history is deliberately not mounted in the shared site API.  It runs as
its own FastAPI/uvicorn process, with OpenAPI/docs and access logging disabled,
and exposes only `POST /api/player-history/query`.  The immutable snapshot is
fully audited once at process construction; requests then verify the pinned
file identity and use indexed, read-only SQLite lookups.

Snapshot construction supports two mutually exclusive inputs. `--live-source`
reads only `data/lcu/games.db` from Git's common primary checkout; linked
worktrees resolve it through strict, non-reparse `.git` metadata. It takes no
path, opens SQLite read-only without `immutable`, validates the exact collector
schema in a consistent transaction, filters queue 2400 plus the configured
patches, and never selects `seed_family`. The live read is bounded to 20 minutes
and 512 MiB of WAL growth, pins the database/WAL/SHM identities, and closes
before the new snapshot is published. It never writes, indexes, checkpoints,
moves, or copies the collector files. `--source <backup>` remains available for
an explicit, stopped offline backup as documented in
`runbooks/player-history.md`.

The public response is a fixed allowlist: `status`, public `snapshot` metadata,
`observed_matches`, `low_sample`, and bounded `histories`.  Unknown and
ambiguous aliases are indistinguishable.  A known alias exposes only games in
which that exact alias appeared; the service never joins renamed aliases by
PUUID.  Eligible unknown/low-sample aliases may be encrypted under the offline
RSA public key and placed in a bounded, lossy quarantine.  That quarantine is
untrusted downstream input and must never be connected directly to a crawler
or live database.

The process requires explicit environment configuration and never creates a
secret or key:

| Variable | Purpose |
| --- | --- |
| `ARAM_PLAYER_HISTORY_SNAPSHOT` | Audited immutable snapshot path. |
| `ARAM_PLAYER_HISTORY_LOOKUP_SECRET_HEX` | Exactly 32 bytes, lowercase hex. |
| `ARAM_PLAYER_HISTORY_CANDIDATE_SECRET_HEX` | Separate dataset-scoped candidate HMAC secret. |
| `ARAM_PLAYER_HISTORY_RATE_SECRET_HEX` | Separate client limiter HMAC secret. |
| `ARAM_PLAYER_HISTORY_RSA_PUBLIC_PEM` | RSA public key PEM, at least 3072 bits; never a private key. |
| `ARAM_PLAYER_HISTORY_QUARANTINE_DB` | Dedicated quarantine SQLite path. |
| `ARAM_PLAYER_HISTORY_RATE_DB` | Dedicated persistent limiter SQLite path. |
| `ARAM_PLAYER_HISTORY_ALLOWED_ORIGINS` | Comma-separated exact origins. Empty means requests without `Origin` only. |
| `ARAM_PLAYER_HISTORY_TRUSTED_PROXY_PEERS` | Comma-separated canonical immediate proxy IPs allowed to supply one canonical `X-Forwarded-For`. |
| `ARAM_PLAYER_HISTORY_RATE_LIMIT_PER_HOUR` | Fixed-window limit; default `20`. |
| `ARAM_PLAYER_HISTORY_HOST` / `ARAM_PLAYER_HISTORY_PORT` | Bind address and port; defaults `127.0.0.1:8766`. |

No real values belong in documentation, shell history, repository files, or
service logs.  Operational build/start/rotation guidance is in
`runbooks/player-history.md`.
