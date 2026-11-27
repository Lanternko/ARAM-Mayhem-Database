"""Static 命定武器 (Weapon of Destiny, augment 3081) page: best first item per champion.

Built from the already-published ``api/tier-list.json`` so shell-only builds can
write it without touching the database.
"""
from __future__ import annotations

import hashlib
import html
import json
from collections import Counter
from pathlib import Path


DESTINY_AUGMENT_ID = "3081"
DESTINY_SLUG = "augments/weapon-of-destiny"
_COPY = {
    "zh-Hant": {
        "prefix": "", "language": "語言", "theme": "切換主題",
        "crumb": "增幅", "title": "命定武器推薦裝備",
        "page_title": "命定武器推薦裝備：每隻英雄第一件出什麼",
        "description": "ARAM 大亂鬥增幅「命定武器」該配哪件裝備？每隻英雄第一件傳說裝備的推薦與勝率差。",
        "lede": "你的第一件傳說裝備會變成命定武器，所以第一件出什麼很重要。下面是每隻英雄的推薦，數字是出這件比不出多贏多少。",
        "common": "最多英雄適合", "champs": "隻英雄", "search": "搜尋英雄",
        "or": "或", "prev": "上版資料", "version": "版本", "empty": "找不到這隻英雄",
    },
    "zh-Hans": {
        "prefix": "/zh-cn", "language": "语言", "theme": "切换主题",
        "crumb": "海克斯", "title": "命定武器推荐装备",
        "page_title": "命定武器推荐装备：每个英雄第一件出什么",
        "description": "海克斯大乱斗「命定武器」该配哪件装备？每个英雄第一件传说装备的推荐与胜率差。",
        "lede": "你的第一件传说装备会变成命定武器，所以第一件出什么很重要。下面是每个英雄的推荐，数字是出这件比不出多赢多少。",
        "common": "最多英雄适合", "champs": "个英雄", "search": "搜索英雄",
        "or": "或", "prev": "上版数据", "version": "版本", "empty": "找不到这个英雄",
    },
    "en": {
        "prefix": "/en", "language": "Language", "theme": "Toggle theme",
        "crumb": "Augments", "title": "Weapon of Destiny: Best Item",
        "page_title": "Weapon of Destiny: Best First Item for Every Champion",
        "description": "Which item to pair with the ARAM Mayhem augment Weapon of Destiny: the best first Legendary item for every champion, with the win rate gain.",
        "lede": "Your first Legendary item becomes the Weapon of Destiny, so the first buy matters. Here is the pick for every champion; the number is how much more often you win with it than without it.",
        "common": "Fits the most champions", "champs": "champions", "search": "Search champion",
        "or": "or", "prev": "previous patch", "version": "Patch", "empty": "No champion found",
    },
}
_LANG_NAMES = ("繁體中文", "简体中文", "English")


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else ''}{x * 100:.1f}%"


def _display_patch(patch_prefix: str) -> str:
    """Riot's public numbering: internal 16.23 is patch 26.23."""
    major, _, minor = str(patch_prefix or "").partition(".")
    return f"{int(major) + 10}.{minor}" if major.isdigit() and minor else str(patch_prefix or "")


def _load(root: Path) -> tuple[dict, dict]:
    data = json.loads((root / "api" / "tier-list.json").read_text(encoding="utf-8"))
    cn_path = root / "api" / "names-zh-cn.json"
    cn = json.loads(cn_path.read_text(encoding="utf-8")) if cn_path.exists() else {}
    return data, cn


def destiny_rows(data: dict, cn: dict, *, locale: str, champion_slugs: set[str] | None = None) -> list[dict]:
    """One row per champion with a recommended item, strongest gain first."""
    items = data.get("itemLut") or {}
    ddv = data.get("ddv") or ""
    cn_champs, cn_items = cn.get("champs") or {}, cn.get("items") or {}

    def item(entry: dict) -> dict | None:
        meta = items.get(str(entry.get("id")))
        if not meta:
            return None
        iid = str(entry["id"])
        name = {"en": meta.get("e"), "zh-Hans": cn_items.get(iid) or meta.get("z")}.get(locale, meta.get("z"))
        return {
            "name": name or meta.get("e") or iid,
            "icon": f"https://ddragon.leagueoflegends.com/cdn/{ddv}/img/item/{iid}.png",
            "lift": float(entry.get("lift") or 0),
            "id": iid,
        }

    rows = []
    for cid, champ in (data.get("champs") or {}).items():
        destiny = champ.get("destinyItem") or {}
        best = item(destiny) if destiny.get("id") else None
        if not best:
            continue
        slug = str(champ.get("alias") or "").lower()
        names = [champ.get("name_zh") or "", champ.get("name_en") or "", cn_champs.get(str(cid)) or ""]
        name = {"en": names[1], "zh-Hans": names[2] or names[0]}.get(locale, names[0])
        rows.append({
            "cid": str(cid), "name": name or names[1], "keys": " ".join(n for n in names if n).lower(),
            "slug": slug if champion_slugs is None or slug in champion_slugs else "",
            "best": best, "alt": item(destiny["alt"]) if (destiny.get("alt") or {}).get("id") else None,
            "prev": bool(destiny.get("prev")),
        })
    rows.sort(key=lambda r: (-r["best"]["lift"], r["name"]))
    return rows


def render_destiny_page(
    data: dict, cn: dict, *, locale: str, site_url: str, css_version: str,
    champion_slugs: set[str] | None = None,
) -> str:
    copy = _COPY[locale]
    esc = html.escape
    origin = site_url.rstrip("/")
    prefix = copy["prefix"]
    path = f"/{DESTINY_SLUG}/"
    canonical = origin + prefix + path
    rows = destiny_rows(data, cn, locale=locale, champion_slugs=champion_slugs)
    aug = (data.get("augs") or {}).get(DESTINY_AUGMENT_ID) or {}
    alternatives = "".join(
        f"<link rel='alternate' hreflang='{lang}' href='{esc(origin + text['prefix'] + path, quote=True)}'>"
        for lang, text in _COPY.items()
    ) + f"<link rel='alternate' hreflang='x-default' href='{esc(origin + path, quote=True)}'>"
    languages = "".join(
        f"<a href='{text['prefix']}{path}' lang='{lang}'"
        + (" aria-current='page'" if lang == locale else "")
        + f">{name}</a>"
        for (lang, text), name in zip(_COPY.items(), _LANG_NAMES)
    )

    def item_html(it: dict, cls: str) -> str:
        return (
            f"<span class='{cls}'><img loading='lazy' width='28' height='28' src='{esc(it['icon'], quote=True)}' alt=''>"
            f"<span class='dw-item-name'>{esc(it['name'])}</span>"
            f"<span class='dw-lift'>{_signed(it['lift'])}</span></span>"
        )

    def row_html(r: dict) -> str:
        champ = (
            f"<img loading='lazy' width='40' height='40' src='/assets/champ-icons/{r['cid']}.webp' alt=''>"
            f"<span class='dw-champ-name'>{esc(r['name'])}</span>"
        )
        champ = (
            f"<a class='dw-champ' href='{prefix}/champions/{r['slug']}/'>{champ}</a>" if r["slug"]
            else f"<span class='dw-champ'>{champ}</span>"
        )
        alt = (
            f"<span class='dw-or'>{copy['or']}</span>" + item_html(r["alt"], "dw-item dw-alt")
        ) if r["alt"] else ""
        prev = f"<span class='dw-prev'>{copy['prev']}</span>" if r["prev"] else ""
        return (
            f"<li class='dw-row' data-k='{esc(r['keys'], quote=True)}'>{champ}"
            f"<span class='dw-picks'>{item_html(r['best'], 'dw-item')}{alt}{prev}</span></li>"
        )

    top = Counter(r["best"]["id"] for r in rows).most_common(4)
    by_id = {r["best"]["id"]: r["best"] for r in rows}
    common = "".join(
        f"<li><img loading='lazy' width='36' height='36' src='{esc(by_id[i]['icon'], quote=True)}' alt=''>"
        f"<span class='dw-common-name'>{esc(by_id[i]['name'])}</span>"
        f"<span class='dw-common-n'>{n} {copy['champs']}</span></li>"
        for i, n in top
    )
    crumbs = json.dumps({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "arammeta", "item": origin + prefix + "/"},
            {"@type": "ListItem", "position": 2, "name": copy["crumb"], "item": origin + prefix + "/augments/"},
            {"@type": "ListItem", "position": 3, "name": copy["title"], "item": canonical},
        ],
    }, ensure_ascii=False)
    icon = f"<img class='dw-aug' width='56' height='56' src='/{esc(aug['icon'], quote=True)}' alt=''>" if aug.get("icon") else ""
    font_region = "SC" if locale == "zh-Hans" else "TC"
    patch = _display_patch(data.get("patch_prefix") or "")
    return f"""<!doctype html>
<html lang='{locale}'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(copy['page_title'])} · arammeta</title>
<meta name='description' content='{esc(copy['description'], quote=True)}'>
<link rel='canonical' href='{esc(canonical, quote=True)}'>{alternatives}
<meta property='og:type' content='website'><meta property='og:site_name' content='arammeta'>
<meta property='og:title' content='{esc(copy['page_title'], quote=True)}'>
<meta property='og:description' content='{esc(copy['description'], quote=True)}'>
<meta property='og:url' content='{esc(canonical, quote=True)}'>
<link rel='icon' href='/favicon.svg' type='image/svg+xml'>
<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Outfit:wght@500;700&amp;family=Noto+Sans+{font_region}:wght@400;500;600;700&amp;display=swap'>
<script>try{{document.documentElement.dataset.theme=localStorage.getItem('aram-mayhem-site-theme')==='light'?'light':'dark'}}catch{{}}</script>
<link rel='stylesheet' href='/assets/site.css?v={css_version}'>
<script type='application/ld+json'>{crumbs}</script>
</head><body class='classic-status-page destiny-page'>
<header class='site-header'><div class='site-header-inner'>
<a class='brand' href='{prefix}/' aria-label='arammeta'><span class='brand-title'><span class='brand-aram'>aram</span><span class='brand-meta'>meta</span></span></a>
<div class='classic-status-controls'>
<nav class='classic-status-languages' aria-label='{copy['language']}'>{languages}</nav>
<button class='classic-status-theme' type='button' aria-label='{copy['theme']}' title='{copy['theme']}'><svg viewBox='0 0 24 24' width='18' height='18' fill='none' stroke='currentColor' stroke-width='1.6' aria-hidden='true'><circle cx='12' cy='12' r='4'></circle><path d='M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'></path></svg></button>
</div></div></header>
<main class='dw-main'>
<p class='dw-crumb'><a href='{prefix}/augments/'>{copy['crumb']}</a><span aria-hidden='true'>›</span>{copy['version']} {esc(patch)}</p>
<div class='dw-head'>{icon}<h1>{esc(copy['title'])}</h1></div>
<p class='dw-lede'>{esc(copy['lede'])}</p>
<section class='dw-common' aria-label='{copy['common']}'><h2>{copy['common']}</h2><ul>{common}</ul></section>
<input class='dw-search' type='search' placeholder='{copy['search']}' aria-label='{copy['search']}' autocomplete='off'>
<ul class='dw-list'>{''.join(row_html(r) for r in rows)}</ul>
<p class='dw-empty' hidden>{copy['empty']}</p>
</main>
<script>document.querySelector('.classic-status-theme').addEventListener('click',()=>{{const theme=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=theme;try{{localStorage.setItem('aram-mayhem-site-theme',theme)}}catch{{}}}});
(()=>{{const q=document.querySelector('.dw-search'),rows=[...document.querySelectorAll('.dw-row')],empty=document.querySelector('.dw-empty');q.addEventListener('input',()=>{{const k=q.value.trim().toLowerCase();let n=0;for(const r of rows){{const hit=!k||r.dataset.k.includes(k);r.hidden=!hit;if(hit)n++}}empty.hidden=n>0}})}})();</script>
</body></html>
"""


def write_destiny_pages(root: Path, *, site_url: str = "https://arammeta.com") -> list[Path]:
    """Write the three locale pages; no-op when the payload has no destiny data."""
    root = Path(root)
    if not (root / "api" / "tier-list.json").exists():
        return []
    data, cn = _load(root)
    if not any((c.get("destinyItem") or {}).get("id") for c in (data.get("champs") or {}).values()):
        return []
    css = (Path(__file__).resolve().parents[3] / "scripts/templates/site.css").read_text(encoding="utf-8")
    version = hashlib.sha1(css.encode("utf-8")).hexdigest()[:12]
    champ_dir = root / "champions"
    slugs = {p.name for p in champ_dir.iterdir() if p.is_dir()} if champ_dir.is_dir() else set()
    written = []
    for locale, copy in _COPY.items():
        path = root / copy["prefix"].lstrip("/") / DESTINY_SLUG / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_destiny_page(
                data, cn, locale=locale, site_url=site_url or "https://arammeta.com",
                css_version=version, champion_slugs=slugs,
            ),
            encoding="utf-8",
        )
        written.append(path)
    return written
