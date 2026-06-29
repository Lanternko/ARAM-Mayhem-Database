---
name: write-column
description: "Author a new 專欄 (column) article for the ARAM Mayhem tier-list site — turn a data finding into a bilingual ARTICLES entry inside scripts/build_tier_list.py, preview it, and hold for deploy. Use whenever the user wants to write/add a column or article from an analysis, write up a finding for the site, or turn data into a site story — even if they don't say the word 'article'. Triggers on: 寫專欄, 寫一篇專欄, 寫一篇, 新增專欄, 加一篇文章, 把這個寫成專欄, write a column, add an article, new column article, write this up for the site, turn this finding into an article, /write-column. Do NOT trigger for publishing/deploying the site (that's deploy-tier-list) or for prose unrelated to the tier-list site."
metadata:
  version: "1.0"
  last_updated: "2026-06-28"
  status: active
  scope: project
---

# write-column — Add a 專欄 article to the ARAM Mayhem site

The site's **專欄** tab is a list of self-contained data stories. Each article is one object in the `ARTICLES` JS array inside `scripts/build_tier_list.py` (search `const ARTICLES = [`). The frontend renders it; there is no separate CMS. This skill captures how to write one well so the numbers are right, the HTML uses the house components, and nothing ships before the user sees it.

**Live URL**: https://arammeta.com/ · article reader view id = `column`.

## The article object (all fields required, bilingual)

```js
{
    id: 'kebab-case-unique',          // also the reader's hash route
    date: '2026-06-28',               // YYYY-MM-DD; today
    kicker_zh: '操作係數', kicker_en: 'Skill-scaling',   // 2–4 char/word category chip
    title_zh: '…', title_en: '…',
    summary_zh: '…', summary_en: '…', // one line; leads with the punchline + a number
    body_zh: `<p>…</p>`,              // HTML string (template literal — see constraints)
    body_en: `<p>…</p>`,
}
```

Insert as the **FIRST** element of `ARTICLES` (newest first — the list renders in array order). Read the two or three existing objects (`id: 'scaling-snowball'`, `'draw-your-sword'`, `'skill-scaling'`) first; copy their rhythm rather than inventing a new shape.

### Body HTML — only these building blocks (they have CSS; nothing else is styled)

- Prose: `<p>…</p>`, section heads `<h2>…</h2>`, lists `<ul><li>…</li></ul>`, emphasis `<b>` / `<i>`.
- A centered formula/callout line: `<p style="text-align:center;font-size:15px;margin:16px 0"><b>…</b></p>`.
- **Champion ranking row** (`.art-rank`): rank, face, name + subtitle, big right-aligned value.
  ```html
  <div class="art-rank"><span class="rk">1</span><img class="art-face" src="{champImg}" alt="汎"><div class="art-meta"><div class="nm">汎</div><div class="sb">55.0% → 60.0% · 23,659 場</div></div><span class="lf" style="color:#3aa0ff">+5.0<small>pp</small></span></div>
  ```
  `.lf` defaults to the accent colour; override inline when sign carries meaning — `#3aa0ff` (good/positive), `#e2574b` (bad/negative), `#9aa3ad` (neutral). Mirror the colours in the matching site chip if one exists.
- **Item build row** (`.art-build` wraps `.art-item`):
  ```html
  <div class="art-build"><div class="art-item"><img src="{itemImg}" alt="蒐集者"><div class="it-nm">蒐集者</div><div class="it-tag">①核心</div></div>…</div>
  ```
- Image URLs (Data Dragon): champion `https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{alias}.png`, item `…/img/item/{itemId}.png`. `{ver}` = `data_dragon_version` from `docs/champion-roles.json` (e.g. `16.13.1`). `{alias}` is the ddragon alias (Wukong→`MonkeyKing`, Bel'Veth→`Belveth`), NOT the display name.

## Workflow

### 1. Find the angle
One finding, one number that surprises. The best columns answer a question a player would actually ask ("does this champ get better in good lobbies?", "who abuses this augment?"). Lead the summary with the payoff.

### 2. Gather exact numbers (don't eyeball, don't trust the console)
The Windows console mojibakes Chinese, so never read champion names off stdout. Compute into a JSON file and Read it back:
```python
# write outputs/article_rows.json, then Read it (UTF-8 clean)
import csv, json
zh    = {int(k): v.get('name_zh') for k,v in json.load(open('docs/api/tier-list.json',encoding='utf-8'))['champs'].items()}
alias = {int(k): v['alias']       for k,v in json.load(open('data/cache/ddragon_champion_byid.json',encoding='utf-8')).items()}
# … pull pp/WR/games from your analysis CSV, sort, pack {alias, zh, …}, json.dump(..., ensure_ascii=False)
```
Sources: `name_zh` ← `docs/api/tier-list.json`; `alias` ← `data/cache/ddragon_champion_byid.json`; `ver` ← `docs/champion-roles.json`. Use a sample-size floor (e.g. ≥800 games) so a row isn't noise.

### 3. Write both languages
Match the house voice (below). Build the `.art-rank` rows by hand from the JSON you just read so every number and name is verbatim.

### 4. Insert into the source
Edit `scripts/build_tier_list.py`, place the object first in `ARTICLES`. **Template-literal constraints** (bodies are backtick strings inside a Python-generated JS blob): no backticks and no `${` inside the HTML, and avoid stray `{` / `}` (inline `style="…"` with no braces is fine).

### 5. Preview and verify (never hand-check by grep alone)
```powershell
python scripts/build_tier_list.py --out outputs/preview_index.html   # inline build → single file, no fetch needed
```
Then drive the preview (server config `tier-list-preview` serves `outputs/` on 8778):
- `preview_start` → navigate to `/preview_index.html` → `location.hash='column'`.
- Click `.view-column .article-card` (your article is first) and assert `document.querySelectorAll('.art-rank').length` matches your row count, spot-check the first/last row text, and `getComputedStyle('.lf')` colours.
- A full-page `preview_screenshot` may time out on the ~20 MB page; DOM/`preview_inspect` checks are the reliable proof. ddragon faces may show as not-loaded in headless — that's the env, not the markup (the URLs are the same ones the other articles use).

### 6. Hold for deploy
Stop here and show the user the preview. Publishing is a separate, explicit step — invoke **deploy-tier-list** (production split build, stage only `docs/index.html` + `docs/api/tier-list.json`, never `git add -A`). Do not push on the strength of "write me a column" alone.

## Voice
- **Data-honest first.** State the effect AND its size; if it's small or noisy, say so. Every column ends with a one-line provenance footer: `資料：本機 games.db · queue 2400（Mayhem）· 版本 … · 每英雄樣本 ≥N 場 · …`.
- **Include the honest caveat** as its own short section — what the number does NOT mean (e.g. "can't read an individual's rank from this"). This is the site's credibility and the user's stated preference.
- Concise, concrete, a little playful in the read ("Sion's 61%→57%"), never hype. Reproduce the textbook intuition when the data matches it — it's a feature, not filler.

## NEVER
- **Never** read champion names from console stdout — cp950 mangles them; round-trip through a JSON file.
- **Never** put backticks or `${…}` inside `body_zh/body_en` — they terminate the JS template literal mid-string and break the whole build.
- **Never** edit `docs/index.html` directly — it's a build artifact; the article lives in `scripts/build_tier_list.py` source (the watchdog rebuilds `docs/` from it).
- **Never** invent CSS class names — only the components above are styled; anything else renders unstyled.
- **Never** publish without an explicit go-ahead — building `outputs/preview_index.html` is review-only; deploying is the deploy-tier-list skill.
- **Never** ship numbers you didn't recompute this session — champion balance and the underlying data drift per patch.
