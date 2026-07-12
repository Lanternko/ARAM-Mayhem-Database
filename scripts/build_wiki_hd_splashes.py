#!/usr/bin/env python3
"""Download League Wiki (Fandom) OriginalSkin HD splashes for draft lock-ins.

Uses leagueoflegends.fandom.com MediaWiki API (official wiki.leagueoflegends.com
returns 403 without a browser session).

Writes:
  docs/assets/draft-splash-hd/{cid}.jpg
  docs/api/draft-splash-hd.json  (local paths preferred over Universe CDN)

Usage:
  python scripts/build_wiki_hd_splashes.py
  python scripts/build_wiki_hd_splashes.py --limit 5   # dry-ish sample
  python scripts/build_wiki_hd_splashes.py --skip-download  # map only
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIER = ROOT / "docs" / "api" / "tier-list.json"
OUT_JSON = ROOT / "docs" / "api" / "draft-splash-hd.json"
OUT_DIR = ROOT / "docs" / "assets" / "draft-splash-hd"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 arammeta-wiki-hd/1.0"
)
API = "https://leagueoflegends.fandom.com/api.php"

# DDragon alias / site name_en → Wiki file title stem (before _OriginalSkin_HD)
ALIAS_TO_WIKI = {
    "MonkeyKing": "Wukong",
    "Renata": "Renata Glasc",
    "Belveth": "Bel'Veth",
    "KSante": "K'Sante",
    "Kaisa": "Kai'Sa",
    "ChoGath": "Cho'Gath",
    "Velkoz": "Vel'Koz",
    "Khazix": "Kha'Zix",
    "RekSai": "Rek'Sai",
    "JarvanIV": "Jarvan IV",
    "LeeSin": "Lee Sin",
    "MasterYi": "Master Yi",
    "MissFortune": "Miss Fortune",
    "TwistedFate": "Twisted Fate",
    "XinZhao": "Xin Zhao",
    "DrMundo": "Dr. Mundo",
    "TahmKench": "Tahm Kench",
    "AurelionSol": "Aurelion Sol",
    "Nunu": "Nunu",
    "FiddleSticks": "Fiddlesticks",
    "Fiddlesticks": "Fiddlesticks",
    "Leblanc": "LeBlanc",
    "Chogath": "Cho'Gath",
}


def http_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": "https://leagueoflegends.fandom.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://leagueoflegends.fandom.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def wiki_imageinfo(title: str) -> dict | None:
    q = urllib.parse.urlencode(
        {
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url|size|dimensions|mime",
            "format": "json",
        }
    )
    try:
        data = http_json(f"{API}?{q}")
    except urllib.error.HTTPError:
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    for p in pages.values():
        if "missing" in p or "imageinfo" not in p:
            return None
        ii = p["imageinfo"][0]
        return {
            "title": p.get("title"),
            "url": ii.get("url"),
            "w": ii.get("width"),
            "h": ii.get("height"),
            "size": ii.get("size"),
            "mime": ii.get("mime"),
        }
    return None


# Extra wiki titles when standard OriginalSkin_HD is missing / renamed.
EXTRA_TITLES = {
    "Ahri": ["File:Ahri OriginalSkin old HD.jpg"],
    "Kayle": ["File:Kayle OriginalSkin old.jpg"],  # may not be true HD
    "Amumu": ["File:Amumu OriginalSkin old.jpg"],
    "Mel": [
        "File:Mel OriginalSkin.jpg",
        "File:Mel_OriginalSkin.jpg",
        "File:Mel OriginalSplash.jpg",
    ],
    "Ambessa": [
        "File:Ambessa OriginalSkin.jpg",
        "File:Ambessa_OriginalSkin.jpg",
        "File:Ambessa Medarda OriginalSkin.jpg",
    ],
    "Locke": [
        "File:Locke OriginalSkin.jpg",
        "File:Locke_OriginalSkin.jpg",
    ],
    "Yunara": [
        "File:Yunara OriginalSkin.jpg",
        "File:Yunara_OriginalSkin.jpg",
    ],
    "Zaahen": [
        "File:Zaahen OriginalSkin.jpg",
        "File:Zaahen_OriginalSkin.jpg",
    ],
}


def candidate_titles(alias: str, name_en: str) -> list[str]:
    stems: list[str] = []
    for s in (
        ALIAS_TO_WIKI.get(alias),
        ALIAS_TO_WIKI.get(name_en),
        name_en,
        alias,
        name_en.replace(" ", ""),
    ):
        if s and s not in stems:
            stems.append(s)
    titles: list[str] = []
    for stem in stems:
        for t in (
            f"File:{stem}_OriginalSkin_HD.jpg",
            f"File:{stem} OriginalSkin HD.jpg",
            f"File:{stem} OriginalSkin old HD.jpg",
            f"File:{stem}_OriginalSkin_old_HD.jpg",
        ):
            if t not in titles:
                titles.append(t)
    for key in (alias, name_en, ALIAS_TO_WIKI.get(alias) or ""):
        for t in EXTRA_TITLES.get(key, []):
            if t not in titles:
                titles.append(t)
    # Non-HD last (often 1215 — only if nothing better exists)
    for stem in stems:
        for t in (
            f"File:{stem}_OriginalSkin.jpg",
            f"File:{stem} OriginalSkin.jpg",
        ):
            if t not in titles:
                titles.append(t)
    return titles


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Only first N champs (debug)")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.15, help="Delay between API calls")
    args = ap.parse_args()

    tier = json.loads(TIER.read_text(encoding="utf-8"))
    champs = list((tier.get("champs") or {}).items())
    champs.sort(key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0)
    if args.limit:
        champs = champs[: args.limit]

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    by_cid: dict[str, dict] = {}
    missing: list[dict] = []
    dim_hist: Counter[str] = Counter()

    # Keep previous Universe entries as fallback if present
    prev = {}
    if OUT_JSON.exists():
        try:
            prev = json.loads(OUT_JSON.read_text(encoding="utf-8")).get("byCid") or {}
        except Exception:
            prev = {}

    for i, (cid, info) in enumerate(champs, 1):
        cid = str(cid)
        alias = str(info.get("alias") or info.get("name_en") or "")
        name_en = str(info.get("name_en") or alias)
        hit = None
        used = None
        for title in candidate_titles(alias, name_en):
            hit = wiki_imageinfo(title)
            if hit and hit.get("url"):
                used = title
                break
            time.sleep(args.sleep)

        if not hit:
            missing.append({"cid": cid, "alias": alias, "name_en": name_en})
            # retain universe fallback if we had one
            if cid in prev:
                by_cid[cid] = {**prev[cid], "source": prev[cid].get("source") or "universe-fallback"}
            print(f"[{i}/{len(champs)}] MISS {cid} {alias}")
            time.sleep(args.sleep)
            continue

        w, h = hit.get("w"), hit.get("h")
        dim_hist[f"{w}x{h}"] += 1
        local_name = f"{cid}.jpg"
        local_path = OUT_DIR / local_name
        rel_url = f"assets/draft-splash-hd/{local_name}"

        if not args.skip_download:
            try:
                data = http_bytes(hit["url"])
                # basic jpeg/png magic
                if len(data) < 10_000:
                    raise RuntimeError(f"too small ({len(data)} bytes)")
                local_path.write_bytes(data)
                size_b = len(data)
            except Exception as e:
                print(f"[{i}/{len(champs)}] DL-FAIL {cid} {alias}: {e}")
                # still record remote wiki URL
                by_cid[cid] = {
                    "url": hit["url"],
                    "w": w,
                    "h": h,
                    "alias": alias,
                    "wikiTitle": used,
                    "source": "fandom-wiki-hd-remote",
                    "error": str(e),
                }
                time.sleep(args.sleep)
                continue
        else:
            size_b = hit.get("size")

        by_cid[cid] = {
            "url": rel_url,  # site-relative for Pages
            "remoteUrl": hit["url"],
            "w": w,
            "h": h,
            "bytes": size_b,
            "alias": alias,
            "wikiTitle": used,
            "source": "fandom-wiki-hd-local",
        }
        print(f"[{i}/{len(champs)}] OK {cid} {alias} {w}x{h} -> {rel_url}")
        time.sleep(args.sleep)

    # Merge: wiki local preferred; for missing without prev, leave absent (site falls back)
    out = {
        "version": 3,
        "source": "leagueoflegends.fandom.com File:{Name}_OriginalSkin_HD.jpg",
        "note": (
            "Wiki HD masters (often 4000–10000px) downloaded to docs/assets/draft-splash-hd/. "
            "Prefer over Universe CDN. missing[] = not found under common HD filenames "
            "(not proof no HD exists)."
        ),
        "assetDir": "assets/draft-splash-hd",
        "byCid": by_cid,
        "missing": missing,
        "stats": {
            "totalMapped": len(by_cid),
            "wikiFound": len(by_cid) - sum(1 for v in by_cid.values() if "universe" in str(v.get("source"))),
            "missing": len(missing),
            "dimHistogram": dict(dim_hist.most_common(30)),
        },
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"\nwrote {OUT_JSON}\n"
        f"mapped={len(by_cid)} missing={len(missing)}\n"
        f"dims={dim_hist.most_common(12)}\n"
        f"miss names={[m['alias'] for m in missing]}"
    )


if __name__ == "__main__":
    main()
