"""Build the 經典 (queue 4310, gameMode JADE) champion win-rate page.

A deliberately standalone, *unlisted* preview page — it is not linked from the
main site nav and carries `noindex`, because 4310 has only a few thousand games
and every number on it is still noise-dominated.  Written as its own script (no
import from build_tier_list / tierlist_engine, per CLAUDE.md) so the production
tier-list pipeline cannot be broken by anything here; the only shared code is
`aram_nn.gamedata`.

Why it is not just "the tier list with queue_id=4310":
  * 經典 ships every champion as a separate ``Jade_*`` id (60000 + base), so all
    metadata joins must go through ``base_champion_id()``.
  * The pool is 60 champions, not 173, and the observed win-rate spread is far
    wider (36%-61%) than Mayhem's, because the sample is small — so the page
    leads with the uncertainty instead of burying it.

Usage:
    python scripts/build_classic_page.py
    python scripts/build_classic_page.py --out docs/classic.html
"""

from __future__ import annotations

import html
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx

from aram_nn.gamedata import base_champion_id, iter_games

CLASSIC_QUEUE_ID = 4310

# Same tier cuts and palette as the main site so the two pages stay readable as
# one product.  They are applied to the *shrunk* win rate, never the raw one.
TIER_ORDER = ["OP", "T1", "T2", "T3", "T4", "T5"]
TIER_CUTS = [("OP", 0.55), ("T1", 0.52), ("T2", 0.50), ("T3", 0.48), ("T4", 0.46)]
TIER_COLOR = {
    "OP": "#d8b8ff",
    "T1": "#ff5a3c",
    "T2": "#f5c518",
    "T3": "#8ec441",
    "T4": "#3aa0ff",
    "T5": "#7a7f8a",
}
TIER_LABEL_BG = dict(TIER_COLOR)
TIER_LABEL_BG["OP"] = (
    "linear-gradient(135deg,"
    "#ffffff 0%,#e7d5ff 18%,#bcd6ff 36%,"
    "#ffd5ec 58%,#fff1c8 78%,#ffffff 100%)"
)

# Flat Beta prior toward 50%, in pseudo-games.  The main site uses k=200 against
# ~40k games per champion, where it is a rounding error; here the median champion
# has ~300 games, so the same k is doing real work — which is the point.  A raw
# 61% off 600 games and a raw 61% off 60 games are not the same claim, and the
# tier a champion lands in should reflect that.
PRIOR_WR = 0.5
PRIOR_GAMES = 200

# Below this the sample cannot support a tier at all; these are listed in the
# table but kept out of the tier board rather than shown as a confident cut.
TIER_BOARD_MIN_GAMES = 50

# 經典 ships its own champion art and its own (old) titles under the Jade_* ids,
# so this page must NOT use the normal Data Dragon portraits — Jade_Kayle is
# 「審判天使」 with the pre-rework icon, not the modern 「正義天使」.  Only
# CommunityDragon exposes those entries, keyed by the Jade id (60000 + base).
CDRAGON_BASE = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global"
)
CHAMPION_SUMMARY = "/v1/champion-summary.json"
# CommunityDragon is unreliable to hotlink from the live site (see the augment
# icons, which are self-hosted for the same reason), so the portraits are copied
# into docs/assets/ at build time and served from our own origin.
ICON_DIR = Path("docs/assets/icons/classic")
ICON_URL_PREFIX = "assets/icons/classic"


def wilson_interval(wins: int, games: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval — the honest error bar on a win rate.

    Preferred over the normal approximation because it stays inside [0,1] and
    keeps its nominal coverage at the sample sizes this page actually has
    (n as low as 50), where Wald intervals are visibly too narrow.
    """
    if games <= 0:
        return (0.0, 1.0)
    p = wins / games
    denom = 1 + z * z / games
    center = (p + z * z / (2 * games)) / denom
    margin = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def assign_tier(wr: float) -> str:
    for tier, cut in TIER_CUTS:
        if wr >= cut:
            return tier
    return "T5"


def load_classic_champion_metadata() -> dict[int, dict]:
    """base champion_id -> the 經典 (Jade_*) name, old title, and portrait id.

    Keyed by the NORMAL champion id so callers can look up straight from
    ``base_champion_id()``, but every value here comes from the Jade entry —
    including ``title_zh``, which is the mode's period-correct title.
    """
    r_en = httpx.get(f"{CDRAGON_BASE}/default{CHAMPION_SUMMARY}", timeout=40)
    r_en.raise_for_status()
    r_zh = httpx.get(f"{CDRAGON_BASE}/zh_tw{CHAMPION_SUMMARY}", timeout=40)

    zh_by_id: dict[int, dict] = {}
    if r_zh.status_code == 200:
        zh_by_id = {int(c["id"]): c for c in r_zh.json() if int(c.get("id", 0)) >= 60000}

    by_id: dict[int, dict] = {}
    for c in r_en.json():
        jade_id = int(c.get("id", 0))
        if jade_id < 60000:
            continue
        base = base_champion_id(jade_id)
        zh = zh_by_id.get(jade_id, {})
        by_id[base] = {
            "jade_id": jade_id,
            "alias": c.get("alias", f"Jade_{base}"),
            "name_zh": zh.get("name") or c.get("name") or str(base),
            "title_zh": zh.get("description") or "",
            "name_en": c.get("name") or str(base),
            "title_en": c.get("description") or "",
            "image": f"{ICON_URL_PREFIX}/{jade_id}.png",
        }
    return by_id


def download_icons(meta: dict[int, dict], icon_dir: Path, refresh: bool) -> int:
    """Copy the Jade portraits into docs/assets/ so the page serves its own icons.

    Already-present files are skipped, so a rebuild costs no network; pass
    ``refresh`` to re-pull them after a patch changes the art.
    """
    icon_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    with httpx.Client(timeout=40) as client:
        for entry in sorted(meta.values(), key=lambda e: e["jade_id"]):
            jade_id = entry["jade_id"]
            dest = icon_dir / f"{jade_id}.png"
            if dest.exists() and not refresh:
                continue
            url = f"{CDRAGON_BASE}/default/v1/champion-icons/{jade_id}.png"
            r = client.get(url)
            if r.status_code != 200 or not r.content:
                click.echo(f"[classic] WARNING: icon {jade_id} -> HTTP {r.status_code}")
                continue
            dest.write_bytes(r.content)
            fetched += 1
    return fetched


def collect_stats(db: Path, patch_prefix: str | None) -> tuple[dict, dict, int, dict]:
    """Per-champion games/wins over queue 4310, plus a per-patch game census."""
    games: dict[int, int] = defaultdict(int)
    wins: dict[int, int] = defaultdict(int)
    per_patch: dict[str, int] = defaultdict(int)
    total = 0
    for g in iter_games(db, queue_id=CLASSIC_QUEUE_ID, patch_prefix=patch_prefix):
        total += 1
        per_patch[g["patch"] or "?"] += 1
        blue_won = int(g["blue_wins"])
        for cid in g["blue_champs"]:
            base = base_champion_id(int(cid))
            games[base] += 1
            wins[base] += blue_won
        for cid in g["red_champs"]:
            base = base_champion_id(int(cid))
            games[base] += 1
            wins[base] += 1 - blue_won
    return games, wins, total, dict(per_patch)


def build_rows(games: dict, wins: dict, total_games: int, meta: dict) -> list[dict]:
    rows = []
    for cid, g in games.items():
        w = wins[cid]
        raw = w / g if g else 0.0
        shrunk = (w + PRIOR_WR * PRIOR_GAMES) / (g + PRIOR_GAMES)
        lo, hi = wilson_interval(w, g)
        m = meta.get(cid, {})
        rows.append({
            "champion_id": cid,
            "alias": m.get("alias", f"#{cid}"),
            "name_zh": m.get("name_zh", f"#{cid}"),
            "name_en": m.get("name_en", f"#{cid}"),
            "title_zh": m.get("title_zh", ""),
            "image": m.get("image", ""),
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "shrunk_wr": shrunk,
            "ci_lo": lo,
            "ci_hi": hi,
            # Presence rate per team: 10 slots per game, 2 teams -> a champion
            # appearing in x% of *teams* is games/total_games/2... but ids are
            # unique per team here, so team-presence = picks / (games * 2).
            "pick_rate": (g / (total_games * 2)) if total_games else 0.0,
            "tier": assign_tier(shrunk),
        })
    rows.sort(key=lambda r: -r["shrunk_wr"])
    return rows


CSS = """
:root{color-scheme:dark;--bg:#0a0b0d;--surface:#101114;--surface-2:#161a20;
--chip-bg:#1a1d21;--text:#e8eaed;--text-muted:#9aa0a6;--text-dim:#6b7280;
--border:rgba(255,255,255,.09);--border-strong:rgba(255,255,255,.15);
--accent:#f5c518;--r-sm:8px;--r-md:12px;--container:1320px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:"Noto Sans TC",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:var(--container);margin:0 auto;padding:24px 16px 80px}
header.top{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:22px;margin:0;letter-spacing:.3px}
.logo-mark{color:var(--accent);font-weight:800}
.unlisted{font-size:11px;font-weight:700;letter-spacing:.5px;padding:3px 8px;
border-radius:999px;background:rgba(245,197,24,.14);color:var(--accent);
border:1px solid rgba(245,197,24,.35)}
.sub{color:var(--text-muted);font-size:13px;margin:0 0 18px}
.banner{border-left:3px solid var(--accent);background:var(--surface);
border-radius:0 var(--r-sm) var(--r-sm) 0;padding:12px 16px;margin:0 0 8px;
font-size:13.5px;line-height:1.7;color:#e2e5e9}
.banner b{color:var(--accent)}
.banner.warn{border-left-color:#ff5a3c}
.banner.warn b{color:#ff8b76}
.banner + .banner{margin-top:8px}
.banner-stack{margin-bottom:22px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 18px}
#q{flex:1;min-width:220px;max-width:360px;background:var(--surface);color:var(--text);
border:1px solid var(--border);border-radius:var(--r-sm);padding:9px 12px;font-size:14px}
#q::placeholder{color:var(--text-dim)}
#q:focus{outline:none;border-color:rgba(245,197,24,.55)}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:var(--r-sm);
overflow:hidden}
.seg button{background:transparent;border:0;color:var(--text-muted);padding:8px 14px;
font-size:13px;cursor:pointer;font-family:inherit}
.seg button.on{background:var(--accent);color:#14110a;font-weight:700}
/* Every tier block carries the SAME padding, even though only OP/T1 paint a
   background wash. .tier-grid derives its column COUNT from container width via
   auto-fill, so padding on only some blocks silently changes how many columns
   fit -- 6px each side was enough to drop OP/T1 from 15 columns to 14 at 1500px,
   making their icons render ~7% larger than every other tier's. */
.tier-block{margin-bottom:22px;position:relative;border-radius:12px;
padding:2px 6px 8px}
.tier-block[data-tier="OP"]{background:radial-gradient(ellipse 70% 60% at 50% 60%,
rgba(216,184,255,.045) 0%,transparent 75%)}
.tier-block[data-tier="T1"]{background:radial-gradient(ellipse 70% 60% at 50% 60%,
rgba(255,90,60,.035) 0%,transparent 75%)}
.tier-heading{display:flex;align-items:center;gap:10px;margin:16px 0 10px;
padding-bottom:8px;font-size:14px;font-weight:600;
border-bottom:1px solid color-mix(in oklab,var(--tier-color,#555) 30%,transparent)}
.tier-pill{position:relative;overflow:hidden;display:inline-flex;align-items:center;
justify-content:center;padding:4px 16px;border-radius:6px;color:#0e1116;
background:var(--tier-bg);font-size:16px;font-weight:700;
text-shadow:0 1px 0 rgba(255,255,255,.25);letter-spacing:.3px}
.tier-pill>span{position:relative;z-index:2}
.tier-count{color:var(--text-muted);font-size:12px;font-weight:400}
.tier-block[data-tier="OP"] .tier-pill{background-size:200% 200%;
animation:prismShift 6s ease-in-out infinite;color:#2a1a4a;
box-shadow:0 0 12px rgba(220,180,255,.55),0 0 28px rgba(170,210,255,.30),
inset 0 0 0 1px rgba(255,255,255,.55);text-shadow:0 1px 0 rgba(255,255,255,.8)}
.tier-block[data-tier="OP"] .tier-pill::before{content:"";position:absolute;inset:0;
background:linear-gradient(115deg,transparent 35%,rgba(255,255,255,.75) 50%,transparent 65%);
background-size:220% 100%;animation:shineSweep 3.2s linear infinite;z-index:1}
@keyframes prismShift{0%{background-position:0% 50%}50%{background-position:100% 50%}
100%{background-position:0% 50%}}
@keyframes shineSweep{from{background-position:220% 0}to{background-position:-120% 0}}
.tier-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(76px,1fr));gap:10px}
.champ{position:relative;aspect-ratio:1/1;border-radius:8px;background:var(--chip-bg);
border:2px solid var(--tier-color,#555);cursor:default;
transition:transform .08s,box-shadow .08s}
.champ img{width:100%;height:100%;object-fit:cover;display:block;border-radius:6px}
@media (hover:hover){.champ:hover{transform:translateY(-3px) scale(1.015);
box-shadow:0 8px 24px -8px rgba(245,197,24,.35);z-index:1}
.champ:hover .name{opacity:1}}
.champ .wr{position:absolute;left:2px;bottom:2px;font-size:10px;font-weight:700;
font-variant-numeric:tabular-nums lining-nums;padding:1px 4px;border-radius:6px;
color:#e6e8eb;background:rgba(14,17,22,.9)}
.champ .n{position:absolute;right:2px;top:2px;font-size:9px;padding:1px 4px;
border-radius:6px;color:#aab0b8;background:rgba(14,17,22,.82);
font-variant-numeric:tabular-nums}
.champ .name{position:absolute;left:0;right:0;bottom:0;padding:2px 4px;font-size:10px;
text-align:center;background:linear-gradient(to top,rgba(0,0,0,.88),rgba(0,0,0,0));
color:#e6e8eb;border-radius:0 0 6px 6px;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;pointer-events:none;opacity:0;transition:opacity .15s}
.champ.thin{opacity:.55}
.champ.thin::after{content:"?";position:absolute;left:3px;top:2px;font-size:10px;
font-weight:800;color:#0e1116;background:#9aa0a6;border-radius:999px;
width:14px;height:14px;display:flex;align-items:center;justify-content:center}
h2.sec{font-size:16px;margin:36px 0 4px;letter-spacing:.3px}
.sec-note{color:var(--text-muted);font-size:12.5px;margin:0 0 12px;line-height:1.7}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--r-md);
background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:720px}
th,td{padding:9px 12px;text-align:right;white-space:nowrap;
border-bottom:1px solid rgba(255,255,255,.05)}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
thead th{position:sticky;top:0;background:var(--surface-2);color:var(--text-muted);
font-weight:600;font-size:12px;cursor:pointer;user-select:none;z-index:2}
thead th:hover{color:var(--text)}
thead th.sorted{color:var(--accent)}
tbody tr:hover{background:rgba(255,255,255,.03)}
td.num{font-variant-numeric:tabular-nums lining-nums}
.tchip{display:inline-block;min-width:26px;text-align:center;padding:1px 6px;
border-radius:4px;font-size:11px;font-weight:700;color:#0e1116}
.cname{display:flex;align-items:center;gap:8px}
.cname img{width:28px;height:28px;border-radius:5px;display:block;flex:none}
.cname small{color:var(--text-dim);font-weight:400}
/* The Jade entries ship the mode's period-correct titles (凱爾 is 審判天使
   here, not 正義天使), which is half the point of a nostalgia mode. */
.cname em{display:block;font-style:normal;font-size:11px;color:var(--text-dim);
margin-top:1px}
/* Error bar: the whole point of the table. Shows the 95% Wilson interval as a
   span against a 30-70% axis, with a hairline at the 50% coin-flip mark. */
.bar{position:relative;width:190px;height:16px;background:rgba(255,255,255,.05);
border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle}
.bar::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;
background:rgba(255,255,255,.28)}
.bar i{position:absolute;top:5px;height:6px;border-radius:3px;background:var(--bc,#9aa0a6);
opacity:.85}
.bar b{position:absolute;top:2px;width:2px;height:12px;background:#fff;border-radius:1px}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--border);
color:var(--text-dim);font-size:12px;line-height:1.9}
footer code{color:var(--text-muted)}
@media (max-width:640px){
.wrap{padding:16px 10px 60px}h1{font-size:18px}
.tier-grid{grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}
.bar{width:110px}}
"""

JS = """
(function(){
  var q=document.getElementById('q');
  var rows=[].slice.call(document.querySelectorAll('tbody tr'));
  var champs=[].slice.call(document.querySelectorAll('.champ'));
  function filter(){
    var v=(q.value||'').trim().toLowerCase();
    champs.forEach(function(c){
      c.style.display=!v||c.dataset.search.indexOf(v)>=0?'':'none';
    });
    rows.forEach(function(r){
      r.style.display=!v||r.dataset.search.indexOf(v)>=0?'':'none';
    });
    document.querySelectorAll('.tier-block').forEach(function(b){
      var any=[].slice.call(b.querySelectorAll('.champ')).some(function(c){
        return c.style.display!=='none';});
      b.style.display=any?'':'none';
    });
  }
  q.addEventListener('input',filter);

  // Sortable table. Numeric columns carry data-v; the rest sort as text.
  var tb=document.querySelector('tbody');
  document.querySelectorAll('thead th').forEach(function(th,i){
    th.addEventListener('click',function(){
      var desc=th.dataset.dir!=='desc';
      document.querySelectorAll('thead th').forEach(function(o){
        o.classList.remove('sorted');delete o.dataset.dir;});
      th.classList.add('sorted');th.dataset.dir=desc?'desc':'asc';
      rows.sort(function(a,b){
        var x=a.cells[i],y=b.cells[i];
        var xv=x.dataset.v,yv=y.dataset.v;
        var r=(xv!==undefined&&yv!==undefined)
          ?parseFloat(xv)-parseFloat(yv)
          :x.textContent.localeCompare(y.textContent,'zh-Hant');
        return desc?-r:r;
      });
      rows.forEach(function(r){tb.appendChild(r);});
    });
  });

  // Board / table view toggle.
  document.querySelectorAll('.seg button').forEach(function(btn){
    btn.addEventListener('click',function(){
      document.querySelectorAll('.seg button').forEach(function(b){
        b.classList.remove('on');});
      btn.classList.add('on');
      var m=btn.dataset.view;
      document.getElementById('board').style.display=m==='table'?'none':'';
      document.getElementById('table-sec').style.display=m==='board'?'none':'';
    });
  });
})();
"""


def render(rows: list[dict], total_games: int, per_patch: dict) -> str:
    board_rows = [r for r in rows if r["games"] >= TIER_BOARD_MIN_GAMES]
    thin = len(rows) - len(board_rows)
    by_tier: dict[str, list] = {t: [] for t in TIER_ORDER}
    for r in board_rows:
        by_tier[r["tier"]].append(r)

    patch_str = "、".join(
        f"{p} ({n:,})" for p, n in sorted(per_patch.items(), key=lambda kv: -kv[1])
    )
    built = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    p: list[str] = []
    p.append("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>")
    p.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    # Unlisted: keep it out of search results and out of the sitemap.
    p.append("<meta name='robots' content='noindex,nofollow'>")
    p.append("<title>經典模式 英雄勝率（內部預覽）· arammeta</title>")
    p.append(f"<style>{CSS}</style></head><body><div class='wrap'>")

    p.append("<header class='top'>")
    p.append("<h1><span class='logo-mark'>aram</span>meta ｜ 經典模式</h1>")
    p.append("<span class='unlisted'>UNLISTED · 內部預覽</span>")
    p.append("</header>")
    p.append(
        "<p class='sub'>queue 4310 · gameMode JADE · 60 英雄固定池 · "
        f"資料截至 {built}</p>"
    )

    p.append("<div class='banner-stack'>")
    p.append(
        "<div class='banner warn'>"
        f"<b>樣本量警告：目前僅 {total_games:,} 場</b>（主站 Mayhem 是 200 萬場級別）。"
        "這頁的每個數字都還在雜訊區間內，<b>不要當成強度結論</b>。"
        "一隻 300 場的英雄，95% 信賴區間大約是 ±5.7pp — "
        "也就是「55%」和「49%」在統計上分不出來。"
        "</div>"
    )
    p.append(
        "<div class='banner'>"
        f"版本分佈：{html.escape(patch_str)}。"
        "資料幾乎全集中在單一版本，<b>跨版本穩定性完全未驗證</b>；"
        "換版之後這張表可能整個重排。"
        "</div>"
    )
    p.append(
        "<div class='banner'>"
        "<b>勝率是「英雄在場的隊伍勝率」，不是英雄強度。</b>"
        "沒有扣掉隊友、分路、玩家自選傾向。"
        "經典有分路 / 視野 / 野區，現有的 position-free 模型不適用，"
        "所以這裡<b>只做描述統計，不做預測</b>。"
        "</div>"
    )
    p.append("</div>")

    p.append("<div class='toolbar'>")
    p.append(
        "<input id='q' type='search' placeholder='搜尋英雄（中 / 英）' "
        "autocomplete='off' spellcheck='false'>"
    )
    p.append(
        "<div class='seg'>"
        "<button data-view='both' class='on'>兩者</button>"
        "<button data-view='board'>Tier 榜</button>"
        "<button data-view='table'>明細表</button>"
        "</div>"
    )
    p.append("</div>")

    # ---- Tier board -------------------------------------------------------
    p.append("<div id='board'>")
    for tier in TIER_ORDER:
        entries = by_tier[tier]
        if not entries:
            continue
        p.append(
            f"<div class='tier-block' data-tier='{tier}' "
            f"style='--tier-color:{TIER_COLOR[tier]}; --tier-bg:{TIER_LABEL_BG[tier]};'>"
        )
        p.append("<h2 class='tier-heading'>")
        p.append(f"<span class='tier-pill'><span>{tier}</span></span>")
        p.append(f"<span class='tier-count'>{len(entries)} 隻</span>")
        p.append("</h2>")
        p.append("<div class='tier-grid'>")
        for r in entries:
            wr = f"{r['shrunk_wr'] * 100:.1f}%"
            search = html.escape(
                f"{r['name_zh']} {r['title_zh']} {r['name_en']} {r['alias']}".lower(),
                quote=True,
            )
            title = (
                f"{r['name_zh']}　{r['title_zh']}\n"
                f"調整後 {wr} · 原始 {r['raw_wr'] * 100:.1f}% · "
                f"{r['games']:,} 場 · 95% CI "
                f"{r['ci_lo'] * 100:.1f}–{r['ci_hi'] * 100:.1f}%"
            )
            p.append(
                f"<div class='champ' data-search=\"{search}\" "
                f"title=\"{html.escape(title, quote=True)}\">"
                f"<img loading='lazy' src='{r['image']}' alt=''>"
                f"<span class='n'>{r['games']}</span>"
                f"<span class='wr'>{wr}</span>"
                f"<span class='name'>{html.escape(r['name_zh'])}</span>"
                f"</div>"
            )
        p.append("</div></div>")
    p.append("</div>")

    # ---- Detail table -----------------------------------------------------
    p.append("<section id='table-sec'>")
    p.append("<h2 class='sec'>完整明細（含誤差範圍）</h2>")
    p.append(
        "<p class='sec-note'>"
        "<b>原始勝率</b>是直接觀察值；<b>調整後</b>把每隻英雄往 50% 收縮 "
        f"{PRIOR_GAMES} 場的先驗，樣本越小拉得越兇 — Tier 榜用的是調整後的值。"
        "<b>誤差條</b>是 95% Wilson 信賴區間（軸 30–70%，白線為觀察值，"
        "中央細線是 50% 硬幣線）：<b>只要色條跨過中央線，就代表這隻英雄"
        "和「五五波」在統計上分不出來</b>。點欄位標題可排序。"
        f"{f' 場次未達 {TIER_BOARD_MIN_GAMES} 的 {thin} 隻不進 Tier 榜。' if thin else ''}"
        "</p>"
    )
    p.append("<div class='table-wrap'><table><thead><tr>")
    for label in [
        "Tier", "英雄", "場次", "原始勝率", "調整後", "95% CI", "誤差範圍", "選用率",
    ]:
        p.append(f"<th>{label}</th>")
    p.append("</tr></thead><tbody>")
    for r in rows:
        search = html.escape(
            f"{r['name_zh']} {r['title_zh']} {r['name_en']} {r['alias']}".lower(),
            quote=True,
        )
        tier = r["tier"] if r["games"] >= TIER_BOARD_MIN_GAMES else "—"
        chip_bg = TIER_COLOR.get(tier, "#3a3f47")
        chip_fg = "#0e1116" if tier != "—" else "#9aa0a6"
        # Error bar geometry, mapped onto a fixed 30%-70% axis so every row is
        # comparable at a glance.
        axis_lo, axis_hi = 0.30, 0.70
        span = axis_hi - axis_lo

        def pos(v: float) -> float:
            return max(0.0, min(100.0, (v - axis_lo) / span * 100))

        left, right = pos(r["ci_lo"]), pos(r["ci_hi"])
        crosses = r["ci_lo"] <= 0.5 <= r["ci_hi"]
        bar_color = "#9aa0a6" if crosses else (
            "#8ec441" if r["raw_wr"] > 0.5 else "#ff5a3c"
        )
        p.append(f"<tr data-search=\"{search}\">")
        p.append(
            f"<td data-v='{r['shrunk_wr']:.6f}'>"
            f"<span class='tchip' style='background:{chip_bg};color:{chip_fg}'>"
            f"{tier}</span></td>"
        )
        p.append(
            f"<td><span class='cname'>"
            f"<img loading='lazy' src='{r['image']}' alt=''>"
            f"<span>{html.escape(r['name_zh'])} "
            f"<small>{html.escape(r['name_en'])}</small>"
            f"<em>{html.escape(r['title_zh'])}</em></span></span></td>"
        )
        p.append(f"<td class='num' data-v='{r['games']}'>{r['games']:,}</td>")
        p.append(
            f"<td class='num' data-v='{r['raw_wr']:.6f}'>{r['raw_wr'] * 100:.1f}%</td>"
        )
        p.append(
            f"<td class='num' data-v='{r['shrunk_wr']:.6f}'>"
            f"{r['shrunk_wr'] * 100:.1f}%</td>"
        )
        p.append(
            f"<td class='num' data-v='{r['ci_lo']:.6f}'>"
            f"{r['ci_lo'] * 100:.1f}–{r['ci_hi'] * 100:.1f}%</td>"
        )
        p.append(
            f"<td data-v='{(r['ci_hi'] - r['ci_lo']):.6f}'>"
            f"<span class='bar' style='--bc:{bar_color}'>"
            f"<i style='left:{left:.2f}%;width:{max(right - left, 1):.2f}%'></i>"
            f"<b style='left:{pos(r['raw_wr']):.2f}%'></b>"
            f"</span></td>"
        )
        p.append(
            f"<td class='num' data-v='{r['pick_rate']:.6f}'>"
            f"{r['pick_rate'] * 100:.1f}%</td>"
        )
        p.append("</tr>")
    p.append("</tbody></table></div></section>")

    p.append("<footer>")
    p.append(
        f"queue 4310（經典 / JADE）· {total_games:,} 場 · "
        f"{len(rows)} 隻英雄 · 頭像與稱號取自 Jade_* 條目（CommunityDragon），"
        f"非現行版本美術<br>"
        f"Tier 切點（調整後勝率）：OP ≥55% · T1 ≥52% · T2 ≥50% · "
        f"T3 ≥48% · T4 ≥46% · T5 &lt;46%<br>"
        f"由 <code>scripts/build_classic_page.py</code> 產生 · {built}<br>"
        "未公開頁面（noindex），不在主站導覽列。"
    )
    p.append("</footer>")

    p.append(f"</div><script>{JS}</script></body></html>")
    return "\n".join(p)


@click.command()
@click.option("--db", default="data/lcu/games.db", type=click.Path(exists=True))
@click.option("--out", default="docs/classic.html", type=click.Path())
@click.option("--patch", "patch_prefix", default="", help="版本前綴過濾；省略＝全收")
@click.option("--icon-dir", default=str(ICON_DIR), type=click.Path())
@click.option("--refresh-icons", is_flag=True, help="重新下載頭像（改版換美術時用）")
def main(db: str, out: str, patch_prefix: str, icon_dir: str, refresh_icons: bool) -> None:
    db_path = Path(db)
    prefix = patch_prefix or None

    click.echo(f"[classic] scanning queue {CLASSIC_QUEUE_ID} from {db_path} ...")
    games, wins, total, per_patch = collect_stats(db_path, prefix)
    if not total:
        raise click.ClickException(f"no queue {CLASSIC_QUEUE_ID} games found in {db}")
    click.echo(f"[classic] {total:,} games, {len(games)} champions")

    click.echo("[classic] fetching Jade_* champion metadata from CommunityDragon ...")
    meta = load_classic_champion_metadata()
    click.echo(f"[classic] {len(meta)} Jade entries")
    unknown = [c for c in games if c not in meta]
    if unknown:
        # base_champion_id() should make this impossible; if it fires, the Jade
        # offset assumption has drifted and the page would silently drop champs.
        click.echo(f"[classic] WARNING: {len(unknown)} unmapped champion ids: {unknown}")

    fetched = download_icons(meta, Path(icon_dir), refresh_icons)
    click.echo(
        f"[classic] icons: {fetched} downloaded, "
        f"{len(list(Path(icon_dir).glob('*.png')))} on disk -> {icon_dir}"
    )

    rows = build_rows(games, wins, total, meta)
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(rows, total, per_patch), encoding="utf-8")

    size_kb = out_path.stat().st_size / 1024
    click.echo(f"[classic] wrote {out_path} ({size_kb:.0f} KB)")
    top = rows[0]
    click.echo(
        f"[classic] top: {top['name_zh']} {top['shrunk_wr'] * 100:.1f}% "
        f"(raw {top['raw_wr'] * 100:.1f}%, {top['games']} games)"
    )


if __name__ == "__main__":
    main()
