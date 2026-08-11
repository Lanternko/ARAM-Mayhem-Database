---
name: update-mayhem-site
description: Refresh, deploy, upload, publish, or 上線 the ARAM Mayhem GitHub Pages tier-list website for this repo. Use for site publishing actions and for requests to inspect or change the deploy SOP.
---

# Update Mayhem Site

Use this skill for the `aram-winrate-nn` GitHub Pages site at https://arammeta.com/.

## Choose the lane

- **Data-only refresh:** no frontend/backend source change is intended. Always use the isolated publisher; local WIP must neither block publishing nor be overwritten.
- **UI/generator deploy:** source changes are intentional. Review and stage those named source files plus the complete generated-site allowlist.

## Data-only refresh

1. Inspect `git status --short --branch` for awareness only. A dirty development worktree is not a blocker.
2. Check the threshold without building:

```powershell
python scripts/publish_static_site.py --once --check-only --patch-prefix auto
```

3. Publish through the default disposable worktree:

```powershell
python scripts/publish_static_site.py --once --patch-prefix auto
```

The publisher fetches `origin/main`, creates a detached temporary worktree, reads the main database through an absolute path, builds there, stages only `DEFAULT_DOC_PATHS`, commits, pushes `HEAD:main`, updates state only after success, and removes the worktree. Never use stash/pop to make room for an automatic data refresh.

4. Verify the reported patch, sample count, canonical/OG URL, JSON build success, commit, and push.

## UI or generator deploy

1. Identify the exact source changes being shipped and run their relevant tests.
2. Run the production build with `https://arammeta.com/`, split payload output, and Meta Pick API `https://api.arammeta.com`.
3. Review both source and generated diffs. Generated output is atomic: use `DEFAULT_DOC_PATHS` from `src/aram_nn/site/static_publish.py` as the single allowlist. It includes the main shell/payload, champion shards, derived APIs, shared assets, route/locale shells, OG image, info pages, and champion roles.
4. Stage only the explicitly selected source files and the generated allowlist. Never use `git add .` or `git add -A`.
5. Commit, sync with `origin/main`, push, and verify the live site.

## Automation

The crawler watchdog starts:

```powershell
python scripts/publish_static_site.py --watch --growth-ratio 0.10 --max-age-hours 12 --threshold 0 --interval-sec 300 --patch-prefix auto
```

Watch mode publishes when growth reaches 10% or the previous successful publish is 12 hours old, whichever happens first. It uses the isolated worktree by default. A failed cycle logs the error, waits, and retries instead of terminating. Use `--no-site-publisher` on the watchdog only when publishing is intentionally disabled.

## Guardrails

- Publish GitHub Pages from `/docs`, never `/site`.
- Production canonical URL is `https://arammeta.com/`.
- A “data-only” build can still change locale/deep-link shells because they embed counts, dates, and cache-bust values. Data-only means no source change, not only two output files.
- Never overwrite or discard unknown WIP to unblock a data refresh; isolation is the unblock mechanism.
- Never stage crawler experiments, raw data, caches, DBs, state files, or unrelated scripts.
- After publishing, report user-visible changes, exact staged outputs, whether skill/source files shipped, verification performed, and remaining local-only changes.
