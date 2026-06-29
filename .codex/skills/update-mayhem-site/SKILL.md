---
name: update-mayhem-site
description: Refresh, deploy, upload, publish, or 上線 the ARAM Mayhem GitHub Pages tier-list website for this repo. Use when the user gives a command such as deploy, upload, 部署, 上傳, 上線, update site, refresh site, rebuild docs/index.html, publish GitHub Pages, or asks for the website update SOP. Do not use for non-action mentions such as asking whether deployment should be considered.
---

# Update Mayhem Site

Use this skill for the `aram-winrate-nn` GitHub Pages tier-list site.

## Workflow

Only proceed when the user's latest message is an action request to deploy/upload/publish the site, or a request for the site deploy SOP. If the user is only discussing whether to deploy, asking "should we deploy?", or mentioning deployment as a possibility, answer the question without running this workflow.

1. Inspect `git status --short --branch` first.
2. Rebuild the split static site from the repo root:

```powershell
python scripts/build_tier_list.py --site-url "https://lanternko.github.io/ARAM-Mayhem-Database/" --payload-out docs/api/tier-list.json --payload-url api/tier-list.json
```

3. Review the generated `docs/index.html` and `docs/api/tier-list.json` diff. Confirm the sample count, patch label, and canonical / OG URL look correct.
4. Stage only intended files. For a normal data refresh, stage only:

```powershell
git add docs/index.html docs/api/tier-list.json
```

If this skill itself was created or updated, also stage only the skill folder:

```powershell
git add .codex/skills/update-mayhem-site
```

5. Commit with the local date or patch in the message, for example:

```powershell
git commit -m "Refresh tier list 2026-05-17"
```

6. Push `main`:

```powershell
git push origin main
```

GitHub Pages redeploys automatically after the push.

## Growth-Based Static Publisher Automation

The crawler watchdog auto-starts the static publisher companion by default:

```powershell
python scripts/mayhem_lcu_watchdog.py --workers 3 ...
```

The companion runs:

```powershell
python scripts/publish_static_site.py --watch --growth-ratio 0.10 --threshold 0 --interval-sec 300
```

It checks local `data/lcu/games.db` every 5 minutes and, once the auto-detected Mayhem patch has at least 10% more games than the last successful publish for that patch, rebuilds the split static site, stages only `docs/index.html` and `docs/api/tier-list.json`, commits, and pushes `main`.  Use `--threshold N` only when a run needs an additional absolute minimum growth floor.

Use `--no-site-publisher` on `mayhem_lcu_watchdog.py` to disable this companion for a specific crawler run.

7. After a successful deploy, always report the shipped changes back to the user. The post-deploy summary must include:
   - the main user-visible changes that were deployed,
   - which files were intentionally staged / deployed,
   - whether any skill files were modified as part of this deploy (`yes` / `no`),
   - what verification ran (for example rebuild success, syntax check, quick visual check),
   - any known caveat or leftover local-only change that was not deployed.

## Guardrails

- Always output to `docs/index.html`; never publish from `/site`.
- Always pass `--site-url "https://lanternko.github.io/ARAM-Mayhem-Database/"` so canonical and OG metadata are populated.
- For production split deploys, pass `--payload-out docs/api/tier-list.json --payload-url api/tier-list.json`.
- Never use `git add -A` or `git add .`.
- Do not stage unrelated WIP files, especially crawler scripts, experiments, raw data, cache files, local DB files, or the static publisher state file.
- If `scripts/build_tier_list.py` has unrelated local edits, do not stage it unless the user explicitly wants the generator changes included.
- Do not end with only "deploy completed"; always leave the user with a short shipped-change recap so they can recover context later.
- Default build settings are owned by `scripts/build_tier_list.py`: queue `2400`, patch prefix `16.10`, `docs/index.html`, `min-games 50`, and `min-pair-games 15`.
