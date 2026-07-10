---
name: deploy-shell
description: "Fast frontend-only deploy of the ARAM Mayhem site: rebuild docs/index.html with --shell-only (reuses existing docs/api/tier-list.json; no win-rate/affinity scan) and push to GitHub Pages. Use when the user only changed CSS/JS/copy/layout/templates, or explicitly wants a shell-only / frontend-only / CSS-only ship without refreshing game data. Triggers on: deploy shell, deploy frontend, deploy css, shell-only deploy, frontend-only deploy, CSS only deploy, 只改 CSS 部署, 只改前端, 前端部署, 不重建資料部署, ship css, ship frontend, /deploy-shell. Do NOT trigger for data/patch refreshes (use deploy-tier-list) or when tier-list.json / radar / axes need regenerating."
metadata:
  version: "1.0"
  last_updated: "2026-07-10"
  status: active
  scope: project
---

# deploy-shell — Frontend-only ship (`--shell-only`)

Ship CSS / JS / copy / layout changes to https://arammeta.com/ **without** rescanning `games.db`.

Full data rebuild (`deploy-tier-list` / `publish_static_site.py`) currently takes **~40–50 min** on the 16.13 sample. Shell-only is **~0.5–2 s**: it re-reads `scripts/templates/site.css` + `site.js`, reuses `docs/api/tier-list.json`, and rewrites only `docs/index.html`.

**Live URL**: https://arammeta.com/

## When to use

| Use this skill | Use `deploy-tier-list` instead |
|---|---|
| `scripts/templates/site.css` / `site.js` edit | New patch / more games / WR numbers wrong |
| HTML shell, layout, copy, SEO meta text in templates/render | `docs/api/tier-list.json` must change |
| Column article already baked into a prior full build; only shell chrome changed | Comp-fit radar or empirical axes need rebuild |
| User says "只改 CSS / 前端 / shell-only" | User says "更新 tier list / 刷資料 / full deploy" |

If unsure whether data changed, ask once. Default: shell-only only when the user is clear the data is fine.

## Canonical safe command (copy exactly)

```powershell
python scripts/build_tier_list.py --shell-only --out docs/index.html `
  --site-url "https://arammeta.com/" `
  --payload-url "api/tier-list.json" `
  --queue 2400 `
  --cloudflare-analytics-token 483cb89af9ee4d1da31ce69cee310b49
```

Optional (only if iterating a pinned past patch for a local preview; live deploys leave auto):

```powershell
# --patch-prefix is only used for the cheap headline game COUNT, not affinity compute
python scripts/build_tier_list.py --shell-only --out docs/index.html `
  --site-url "https://arammeta.com/" `
  --payload-url "api/tier-list.json" `
  --queue 2400 `
  --patch-prefix auto `
  --cloudflare-analytics-token 483cb89af9ee4d1da31ce69cee310b49
```

### Why every flag is required

Bare `--shell-only --out docs/index.html` **silently** drops production metadata because `render_html` gets empty `site_url` / `cloudflare_analytics_token`:

| Missing flag | Live damage |
|---|---|
| `--site-url` | No `og:url`, `og:image`, `twitter:image`, `<link rel=canonical>` → Threads/social previews break (~28% of traffic) |
| `--cloudflare-analytics-token` | No `cloudflareinsights` beacon → daily view stats (~2k/day) go dark |
| `--payload-url` | Shell may not fetch the split payload correctly |

The git `--stat` still looks tiny (CSS lines only), so the loss is easy to miss. **Never** ship a bare shell-only build.

Token is public by design (ships in client HTML). Same constant as `DEFAULT_CF_ANALYTICS_TOKEN` in `src/aram_nn/site/static_publish.py`.

## Pre-flight (silent unless fail)

1. Branch is `main`; `origin` is `Lanternko/ARAM-Mayhem-Database` (or the redirecting old name — warn if still old).
2. `docs/api/tier-list.json` exists and non-empty (shell-only **requires** it; refuse and tell user to run full `deploy-tier-list` first if missing).
3. Source edits actually land in what shell embeds:
   - CSS/JS → `scripts/templates/site.css`, `scripts/templates/site.js` (inlined at build time)
   - Do **not** hand-edit `docs/index.html` and commit — next rebuild clobbers it
4. Collector running is OK (shell only does a cheap `COUNT(*)` for the headline; WAL readers are fine).
5. Do **not** refuse solely because `docs/index.html` is dirty — shell-only will overwrite it. Do refuse if the user has **unrelated hand-edits inside** `docs/index.html` they want to keep (unlikely; treat as build artifact).

## Mandatory metadata gate (before commit)

After shell-only, **abort commit** unless all three tokens are present in `docs/index.html`:

```powershell
# PowerShell
$html = Get-Content docs/index.html -Raw
@('og:image','rel=''canonical''','cloudflareinsights') | ForEach-Object {
  if ($html -notmatch [regex]::Escape($_)) { throw "MISSING metadata token: $_ — refuse to commit" }
  "OK $_"
}
```

Or bash:

```bash
for t in "og:image" "rel='canonical'" "cloudflareinsights"; do
  grep -q "$t" docs/index.html || { echo "MISSING $t — refuse to commit"; exit 1; }
done
```

Expected diffs vs `HEAD`:
- CSS/JS/copy you changed (inlined into the HTML)
- Headline game count may tick up (live `COUNT(*)` vs last full publish) — normal
- **Must not** lose `og:image` / `canonical` / `cloudflareinsights`

## Stage / commit / push

```powershell
# ONLY the shell (+ clean-path deep-link stubs). Never re-stage payload / radar /
# axes — they are untouched and re-adding them risks mixing in accidental local edits.
git add docs/index.html docs/404.html
git add docs/augments docs/changes docs/column docs/settings

# If template sources changed and should stay in sync on main, stage them too
# (same commit). Skip if they are unrelated WIP.
git add scripts/templates/site.css scripts/templates/site.js

git status
git diff --cached --stat
# Review: should be docs/index.html ± templates. No data/, no random scripts/.

git commit -m "Ship frontend shell: <one-line reason>"
git push origin main
```

Commit message examples:
- `Ship frontend shell: fix mobile tier-list overflow`
- `Ship frontend shell: CSS-only polish for detail header`

## After push

> Push 完成 (`<short_sha>`). Pages 約 30–60 秒後更新；重新整理 https://arammeta.com/ 即可。  
> 這次是 **shell-only**，遊戲數據 / radar / axes **沒有**重算。

Do not poll the live URL (CDN cache lies). Actions: https://github.com/Lanternko/ARAM-Mayhem-Database/actions

## NEVER

- **Never** bare `--shell-only` without `--site-url` + `--payload-url` + `--cloudflare-analytics-token`.
- **Never** `git add -A` / `git add .`.
- **Never** stage `docs/api/tier-list.json`, `champ-archetype-fit.json`, or `champ-empirical-axes.json` on a shell deploy (data unchanged; avoid accidental dirty copies).
- **Never** include `data/` (PII / LCU puuids).
- **Never** force-push `main`.
- **Never** hand-edit and commit `docs/index.html` as the source of truth — templates are.
- **Never** use this skill when the user asked to refresh patch data / WR / affinities — hand off to `deploy-tier-list`.
- **Never** promise radar/axes updates from shell-only — those need the full pipeline + parquet.

## Related

- Full data + split payload deploy: `.claude/skills/deploy-tier-list/SKILL.md`
- Auto publisher (watchdog): `python scripts/publish_static_site.py --once --force` (always full rebuild path)
- Templates live at: `scripts/templates/site.css`, `scripts/templates/site.js`
