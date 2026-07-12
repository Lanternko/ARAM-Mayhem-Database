#!/usr/bin/env python3
"""Build docs/api/draft-splash-hd.json — high-res splash URLs for draft lock-ins.

Sources (investigated 2026-07):
  - Data Dragon splash is fixed at **1215×717** (Riot's public delivery size).
  - Riot Universe CMS (cmsassets.rgpub.io/sanity) hosts higher masters for most
    champions — often 1920×1080; some 2–5k. Browse JSON `width/height` is often
    **wrong/stale**; trust the `-WxH` in the filename / actual fetch instead.
  - Sanity image pipeline can re-encode with `?w=2560&q=90` for sharper zoom.
  - CommunityDragon uncentered splash is also 1215×717 (not true HD).
  - Wiki "HD" artist files exist but are not openly enumerable (403 without session).

Re-run when new champions ship:
  python scripts/build_draft_splash_hd.py
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "api" / "draft-splash-hd.json"
TIER = ROOT / "docs" / "api" / "tier-list.json"
UA = "Mozilla/5.0 (compatible; arammeta-splash-hd/1.1)"
BROWSE = "https://universe-meeps.leagueoflegends.com/v1/en_us/champion-browse/index.json"
CHAMP_JSON = "https://universe-meeps.leagueoflegends.com/v1/en_us/champions/{slug}/index.json"

# site / DDragon alias → Universe slug
ALIAS_TO_SLUG = {
    "renata": "renataglasc",
    "wukong": "monkeyking",
    "monkeyking": "monkeyking",
    "nunu": "nunu",
    "nunuwillard": "nunu",
    "belveth": "belveth",
}

DIM_RE = re.compile(r"-(\d{3,5})x(\d{3,5})\.(?:jpe?g|png|webp)(?:\?|$)", re.I)


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def clean_url(uri: str | None) -> str | None:
    if not uri:
        return None
    return str(uri).split("?", 1)[0]


def dims_from_url(url: str) -> tuple[int | None, int | None]:
    m = DIM_RE.search(url or "")
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def with_quality(url: str) -> str:
    """Sanity CDN: request larger delivery + high JPEG quality for zoomed lock-ins."""
    if "cmsassets.rgpub.io" in url and "sanity/images" in url:
        # w=2560: Teemo/Aatrox deliver 2560×1440; pure 1215 masters upscale soft but still ok.
        return url + "?w=2560&q=90&auto=format"
    return url


def entry_from_image(img: dict, *, slug: str, name: str | None) -> dict | None:
    uri = clean_url(img.get("uri") if isinstance(img, dict) else None)
    if not uri:
        return None
    fw, fh = dims_from_url(uri)
    # Prefer filename dims over browse metadata (often stale / wrong).
    w = fw if fw else (img.get("width") if isinstance(img, dict) else None)
    h = fh if fh else (img.get("height") if isinstance(img, dict) else None)
    return {"url": uri, "w": w, "h": h, "slug": slug, "name": name}


def main() -> None:
    browse = get_json(BROWSE)
    tier = json.loads(TIER.read_text(encoding="utf-8")) if TIER.exists() else {"champs": {}}

    by_slug: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for ch in browse.get("champions") or []:
        slug = str(ch.get("slug") or "").lower()
        name = str(ch.get("name") or "")
        img = ch.get("image") or {}
        entry = entry_from_image(img, slug=slug, name=name)
        if not slug or not entry:
            continue
        by_slug[slug] = entry
        if name:
            by_name[name.replace(" ", "").lower()] = entry
            by_name[name.lower()] = entry

    by_cid: dict[str, dict] = {}
    missing: list[str] = []

    for cid, info in (tier.get("champs") or {}).items():
        alias = str(info.get("alias") or info.get("name_en") or "").replace(" ", "")
        name_en = str(info.get("name_en") or "")
        key = alias.lower()
        slug = ALIAS_TO_SLUG.get(key, key)
        entry = (
            by_slug.get(slug)
            or by_name.get(key)
            or by_name.get(name_en.replace(" ", "").lower())
            or by_name.get(name_en.lower())
        )
        if not entry:
            try:
                page = get_json(CHAMP_JSON.format(slug=slug))
                img = (page.get("champion") or {}).get("image") or {}
                entry = entry_from_image(img, slug=slug, name=name_en or alias)
                if entry:
                    by_slug[slug] = entry
            except Exception:
                entry = None
        if entry:
            by_cid[str(cid)] = {
                "url": with_quality(entry["url"]),
                "w": entry.get("w"),
                "h": entry.get("h"),
                "alias": alias,
                "slug": entry.get("slug"),
            }
        else:
            missing.append(f"{cid}:{alias}")

    n_hi = sum(1 for e in by_cid.values() if (e.get("w") or 0) >= 1800)
    n_mid = sum(1 for e in by_cid.values() if 1000 <= (e.get("w") or 0) < 1800)
    out = {
        "version": 2,
        "source": "universe-meeps champion-browse (filename dims + Sanity ?w=2560&q=90)",
        "note": (
            "Data Dragon public splash is only 1215×717. Universe CMS often has "
            "1920+ masters; we parse -WxH from the asset URL (browse metadata lies) "
            "and request Sanity delivery at w=2560 for sharper draft lock-in zoom."
        ),
        "byCid": by_cid,
        "missing": missing,
        "stats": {
            "total": len(by_cid),
            "width_ge_1800": n_hi,
            "width_1k_to_1800": n_mid,
            "missing": len(missing),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT}\n"
        f"  champs={len(by_cid)}  w>=1800={n_hi}  1k-1800={n_mid}  missing={missing}"
    )


if __name__ == "__main__":
    main()
