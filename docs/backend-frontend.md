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

For installed editable entry points:

```powershell
aram-site-api --host 0.0.0.0 --port 8000
aram-site-sync --api-url https://your-vm.example --watch
```

## Split Static Site Build

Generate HTML plus an external data payload:

```powershell
python scripts/build_tier_list.py `
  --site-url "https://lanternko.github.io/ARAM-Mayhem-Database/" `
  --payload-out docs/api/tier-list.json `
  --payload-url api/tier-list.json
```

The generated `docs/index.html` keeps the existing UI but loads `DATA` from `docs/api/tier-list.json`. Omitting `--payload-url` preserves the old fully-inline HTML behavior.

## Privacy Contract

The backend database is intentionally narrower than the collector DB. It stores only:

- `game_id`, `queue_id`, `patch`
- sorted champion IDs by side
- blue-side win flag, duration, creation time
- captured timestamp
- public participant stats needed for augments/items

Bulk ingest rejects `participants_json` if it contains PUUID-like UUIDs or private field names such as `puuid`, `summonerName`, or `riotId`.
