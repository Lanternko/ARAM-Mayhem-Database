---
name: deploy-tier-list
description: "Full data rebuild of docs/index.html + docs/api/*.json from data/lcu/games.db and publish to GitHub Pages (Lanternko/ARAM-Mayhem-Database, branch main, folder /docs). Use when the user wants to refresh patch stats, ship new game data, or push a complete tier-list rebuild. Triggers on: deploy tier list, publish tier list, update tier list site, refresh site, rebuild site, push to pages, ship to pages, ship tier list, full deploy, 更新網站, 更新 tier list, 重新部署, 重建網站, 把 tier list 推上去, 部署 tier list, /deploy-tier-list. Do NOT trigger for CSS/JS/copy-only ships — use deploy-shell instead."
metadata:
  version: "1.2"
  last_updated: "2026-07-10"
  status: active
  scope: project
---

# deploy-tier-list — Rebuild & ship `docs/index.html`

ARAM Mayhem tier-list site is published via **GitHub Pages → branch `main` → folder `/docs`**. The whole pipeline is one Python build + three git commands. This skill encapsulates that flow so we don't paste the canonical URL by hand every time.

**Live URL**: https://arammeta.com/

## What it does

1. Re-runs `scripts/build_tier_list.py` against `data/lcu/games.db` with the **canonical site URL hardcoded** (`--site-url https://arammeta.com/`) so OG / Twitter / `<link rel=canonical>` are correct.
2. Stages the split-build site artifacts — `docs/index.html`, `docs/api/tier-list.json`, **the empirical comp-fit radar `docs/api/champ-archetype-fit.json`, and the empirical ability axes `docs/api/champ-empirical-axes.json`** (plus any newly downloaded `docs/assets/icons/`) — never `data/`, never the script (unless the user is also pushing code changes), never unrelated untracked WIP.
3. Commits with a date- or patch-stamped message.
4. Pushes to `origin main`. Pages auto-rebuilds in ~30–60 s.

## When to invoke

Auto-trigger on any rephrase of "deploy/update/publish the tier list site" **when game data must refresh**. Explicit `/deploy-tier-list` always invokes.

**Hand off to `deploy-shell`** (do not run the full rebuild) when:
- The user only changed CSS / JS / copy / layout / `scripts/templates/*`.
- They say shell-only / frontend-only / CSS-only / 只改前端 / 不重建資料.
- See `.claude/skills/deploy-shell/SKILL.md`.

**Don't trigger** when:
- The user only changed templates and hasn't asked to push — just run a local `--shell-only` preview so they can review.
- The user is asking about deploy *mechanics* ("how do I…") — answer with text, don't push.
- `data/lcu/games.db` is mid-write by a running collector — for a *full* data rebuild, prefer waiting or warn; concurrent WAL readers usually work (the watchdog already publishes while snowball runs) but refuse if the user wants a guaranteed clean snapshot.

## The canonical command

```powershell
# 1. Rebuild (always include --site-url; do NOT omit, or canonical/OG tags break)
python scripts/build_tier_list.py --site-url "https://arammeta.com/"
```

Defaults the script already applies (do not override unless the user asks):
- `--db data/lcu/games.db`
- `--queue 2400` (Mayhem)
- `--patch-prefix auto` — the default. Auto-detects the newest patch (latest `major.minor` with ≥1000 games, ranked by most-recent game time) from the DB via `latest_patch_prefix`. **Never hardcode a patch number here** — `auto` can't go stale across patch bumps. Pass `""` for all-patches, or an explicit `16.NN` only to pin a *past* patch on purpose.
- `--out docs/index.html`
- `--min-games 50`, `--min-pair-games 15`
- `--build-date` = today

Leave `--patch-prefix` at its `auto` default for normal deploys — it already targets the current patch. Only pass an explicit `16.NN` when the user wants a *specific past* patch ("用 16.11 重新出一份"); never pin the current patch by hand (that's what went stale before). Override `--queue 450` if the user explicitly asks for ARAM, but warn that ARAM sample is currently tiny (~139 games).

## Frontend-only deploys → use `deploy-shell`

For a CSS / JS / copy fix the data hasn't changed. A full rebuild is currently **~40–50 min** (16.13 has 500k+ games × multiple full DB scans); `--shell-only` reuses `docs/api/tier-list.json` in **~0.5–2 s**.

**Do not improvise shell-only from this skill.** Follow `.claude/skills/deploy-shell/SKILL.md` end-to-end (canonical flag set + metadata gate + stage only `docs/index.html`). Bare `--shell-only` without `--site-url` / `--cloudflare-analytics-token` **silently** strips OG/canonical tags and the Cloudflare analytics beacon.

## Comp-fit radar + empirical ability axes (split deploys only)

The live site fetches **two** extra payloads separately, both derived from the pooled parquet and both rebuilt **AFTER** the tier list (they read the freshly rebuilt `docs/api/tier-list.json`):

- `docs/api/champ-archetype-fit.json` — the empirical "適配陣型" radar. If a deploy omits it the fetch 404s and every champion silently falls back to the heuristic ("估計") radar. Reads the split payload's `champ.comp` vectors.
- `docs/api/champ-empirical-axes.json` — the empirical `後期` / `滾雪球` ability bars (scaling = WR(long games) − WR(short); snowball = killing-spree / multi-kill blend). If omitted the fetch 404s and those two bars silently read 0 for every champion. Reads `tier-list.json` only for champion names.

Both are **required** files. The split build is the one the automated watchdog uses, so a manual deploy should match it:

```powershell
# 1b. Split build (writes docs/api/tier-list.json + docs/index.html shell)
python scripts/build_tier_list.py --site-url "https://arammeta.com/" --payload-out docs/api/tier-list.json --payload-url api/tier-list.json

# 1c. Regenerate the comp-fit radar from the just-rebuilt tier-list.json.
#     Prefer the auto-maintained pool (the +25% recommender refresh keeps it fresh);
#     fall back to data/raw/mayhem_pooled_16_10_12.parquet if it is absent.
python scripts/build_champ_archetype_fit.py --data data/raw/mayhem_pooled_auto.parquet --payload docs/api/tier-list.json --out docs/api/champ-archetype-fit.json

# 1d. Regenerate the empirical ability axes (scaling + snowball) from the same pool.
#     No --payload flag: the generator reads docs/api/tier-list.json from a fixed path.
python scripts/build_champ_empirical_axes.py --data data/raw/mayhem_pooled_auto.parquet --out docs/api/champ-empirical-axes.json
```

The watchdog's `publish_static_site.py` does all of these automatically before each auto-publish (`src/aram_nn/site/static_publish.py`); you only run them by hand for an off-cycle manual deploy.

## Stage / commit / push (the safe subset)

```powershell
# 2. Stage ONLY the regenerated site artifacts. Never `git add .` / `git add -A` —
#    the working tree always carries unrelated WIP scripts under scripts/.
#    Inline single-file build: just docs/index.html.
#    Split build (the deploy the watchdog uses): all four artifacts below,
#    plus docs/assets/icons if new self-hosted icons were downloaded this round.
git add docs/index.html docs/404.html docs/augments docs/changes docs/column docs/settings
git add docs/api/tier-list.json docs/api/champ-archetype-fit.json docs/api/champ-empirical-axes.json

# 3. Commit. Message convention: "Refresh tier list <date>" or
#    "Refresh tier list <patch>" if a new patch is the reason.
git commit -m "Refresh tier list 2026-05-15"

# 4. Push to main; Pages auto-deploys.
git push origin main
```

If the script itself changed and the user wants those changes shipped together, add `scripts/build_tier_list.py` to the same commit — but check `git diff --cached` first so we don't accidentally include unrelated WIP.

## Pre-flight checks (do these silently, only surface failures)

| Check | What goes wrong if skipped |
|---|---|
| `data/lcu/games.db` exists and not zero-size | Script crashes with cryptic SQLite error |
| `data/cache/kiwi.bin.json` & `lol_stringtable_zh_tw.json` present **or** internet reachable | Augment descriptions silently come out empty |
| Working tree clean wrt `docs/` (no manual edits) | `git add docs/index.html` would commit stale hand-edits |
| Current branch is `main` | Pushing a non-main branch won't trigger Pages |
| `origin` URL is `Lanternko/ARAM-Mayhem-Database` | Old `ARAM-mayhem-collector` URL still works via redirect but warn the user |

## After push — verification

Pages takes 30–60 s to rebuild. Tell the user:

> Push 完成 (`<short_sha>`). Pages 會在 30–60 秒內重新部署，重新整理 https://arammeta.com/ 即可看到新版。

Do **not** poll or curl the live URL — Pages CDN caches aggressively and a 200 right after push doesn't prove the new build is live. Trust the GitHub Actions log if the user wants confirmation: https://github.com/Lanternko/ARAM-Mayhem-Database/actions

## NEVER

- **Never** `git add -A` / `git add .` — staging area always carries unrelated WIP across many files.
- **Never** include `data/` in commits — it's in `.gitignore` for a reason (LCU puuids inside).
- **Never** force-push to `main` — Pages live URL would temporarily 404 and Discord/Twitter previews could break.
- **Never** edit `docs/index.html` by hand and commit — it's a build artifact; changes get clobbered next rebuild.
- **Never** drop `--site-url` — `og:url` / `<link rel=canonical>` would point to nothing, breaking social previews.
- **Never** deploy a bare `--shell-only` build from this skill — hand off to `deploy-shell`, which owns the safe flag set + metadata gate.
- **Never** swap `/docs` for `/site` — GitHub Pages only supports `/(root)` and `/docs` in branch mode (we hit this once already; see commit `3806df6`).
- **Never** ship a split deploy that updates `docs/api/tier-list.json` but forgets `docs/api/champ-archetype-fit.json` — the comp-fit radar would 404 and every champion falls back to the heuristic estimate. Rebuild it from the fresh tier-list.json and stage it in the same commit.
- **Never** ship a split deploy that forgets `docs/api/champ-empirical-axes.json` — the `後期` / `滾雪球` ability bars would 404 and silently read 0 for every champion. Rebuild it from the fresh tier-list.json and stage it in the same commit (it is the fourth required artifact alongside the HTML, tier-list.json, and the comp-fit radar).
