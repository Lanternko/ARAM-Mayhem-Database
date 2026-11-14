#!/usr/bin/env python3
"""Build clean, high-res square + circle champion icons from the original splash art.

DDragon's ``img/champion/{alias}.png`` is a 120px crop with a dark frame baked
into its edge; rounding that in CSS shows the frame as chipped corners.  This
script finds where Riot cropped each icon from the splash (multi-scale template
match), then re-crops that framing from the full-resolution art:

  docs/assets/champ-icons/{cid}.webp         square, SIZE px, opaque
  docs/assets/champ-icons/{cid}-circle.webp  circle, SIZE px, alpha (anti-aliased)
  docs/assets/champ-icons/manifest.json      per-champion source + match score

The framing is always the old DDragon icon's: the site zooms every icon to
114-125% exactly as it did with the 120px icons, so only the pixels change.
Sources, largest first: HD wiki splash (docs/assets/draft-splash-hd, ~5760px),
Riot Universe splash (often 1920px), DDragon splash (1215px).  The icon is
matched in each; the largest source scoring >= GOOD_SCORE wins, else the best.
Champions whose icon art is in no splash are cropped by hand in
scripts/site_data/champion_icon_crops.json ({cid: {source, cx, cy, side}}, normalised:
cx/side by width, cy by height; side is what the site should *show*, so it is
widened by OLD_ZOOM to cancel the site's zoom).  Anything else that does not
match falls back to the DDragon icon itself.

OpenCV is not a project dependency; run it in an ephemeral env:

  uv run --no-project --with opencv-python-headless --with pillow --with numpy \
      python scripts/build_champion_icons.py [--only 91,17] [--force] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
TIER = ROOT / "docs" / "api" / "tier-list.json"
HD_DIR = ROOT / "docs" / "assets" / "draft-splash-hd"
OUT_DIR = ROOT / "docs" / "assets" / "champ-icons"
CACHE = ROOT / "data" / "cache" / "champion-icon-src"
DDRAGON = "https://ddragon.leagueoflegends.com/cdn"
UNIVERSE = "https://universe-meeps.leagueoflegends.com/v1/en_us/champions/{slug}/index.json"
UNIVERSE_SLUG = {"monkeyking": "monkeyking", "renata": "renataglasc", "nunu": "nunu"}
CDRAGON_GAME_DATA = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default"

SIZE = 256            # output edge in px (>=2x the largest on-site render)
ICON_INSET = 0.07     # strip DDragon's baked frame before matching
COARSE_W = 640        # splash width for the scale sweep
FINE_W = 1600         # splash width for the refinement
MIN_SCORE = 0.55      # icon match below this is unreliable (visually checked: >=0.55 all right)
MIN_HIRES = 128       # source side below this is flagged LOW-RES (2x of a 64px render)
MIN_FRAC = 0.10       # a match smaller than this fraction of splash height is a false hit
CIRCLE_SCALE = 1.0    # circle diameter relative to the square crop


GOOD_SCORE = 0.80     # icon match this good is the same art; prefer the largest such source
OLD_ZOOM = 1.14       # site CSS zoom on icons (114%, -7% margin): manual crops are widened by it
CROPS = ROOT / "scripts" / "site_data" / "champion_icon_crops.json"


@dataclass
class Match:
    cx: float
    cy: float
    side: float
    score: float


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "arammeta-icon-build/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            dest.write_bytes(resp.read())
    return dest


def cdragon_tile_url(cid: str) -> str:
    """Base-skin tile URL from CommunityDragon's per-champion JSON (tilePath)."""
    meta = fetch(f"{CDRAGON_GAME_DATA}/v1/champions/{cid}.json", CACHE / "cdragon" / f"{cid}.json")
    tile_path = json.loads(meta.read_text(encoding="utf-8"))["skins"][0]["tilePath"]
    return CDRAGON_GAME_DATA + "/" + tile_path.split("/lol-game-data/assets/", 1)[1].lower()


def universe_splash(alias: str) -> Path | None:
    slug = UNIVERSE_SLUG.get(alias.lower(), alias.lower())
    try:
        meta = fetch(UNIVERSE.format(slug=slug), CACHE / "universe" / f"{slug}.json")
        uri = json.loads(meta.read_text(encoding="utf-8"))["champion"]["image"]["uri"].split("?", 1)[0]
        return fetch(uri, CACHE / "universe" / f"{slug}{Path(uri).suffix}")
    except Exception:  # Universe is best-effort; the chain has other sources
        return None


def to_bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def resize_w(img: np.ndarray, width: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    k = min(1.0, width / w)
    if k == 1.0:
        return img, 1.0
    return cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA), k


def best_at(hay: np.ndarray, tpl: np.ndarray, sizes, region=None):
    """Best (score, x, y, size) of ``tpl`` scaled to each size, optionally within a region."""
    ox = oy = 0
    if region is not None:
        x0, y0, x1, y1 = (max(0, int(v)) for v in region)
        hay = hay[y0:y1, x0:x1]
        ox, oy = x0, y0
    best = (-2.0, 0, 0, 0)
    for s in sizes:
        s = int(round(s))
        if s < 16 or s >= min(hay.shape[:2]):
            continue
        res = cv2.matchTemplate(hay, cv2.resize(tpl, (s, s), interpolation=cv2.INTER_AREA),
                                cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx > best[0]:
            best = (float(mx), loc[0] + ox, loc[1] + oy, s)
    return best


def locate(tpl_full: np.ndarray, splash: np.ndarray, inset: float,
           min_frac: float = MIN_FRAC) -> Match:
    """Where (centre, side, in ``splash`` pixels) the square ``tpl_full`` sits."""
    n = tpl_full.shape[0]
    pad = round(n * inset)
    tpl = tpl_full[pad:n - pad, pad:n - pad]
    frac = tpl.shape[0] / n
    coarse, kc = resize_w(splash, COARSE_W)
    sh = coarse.shape[0]
    score, x, y, s = best_at(coarse, tpl, np.geomspace(sh * min_frac, sh * 0.98, 40))
    fine, kf = resize_w(splash, FINE_W)
    r = kf / kc
    fx, fy, fs = x * r, y * r, s * r
    region = (fx - fs * 0.15, fy - fs * 0.15, fx + fs * 1.15, fy + fs * 1.15)
    f = best_at(fine, tpl, np.linspace(fs * 0.93, fs * 1.07, 11), region)
    if f[0] >= score:
        score, x, y, s = f
        k = kf
    else:
        k = kc
    side = s / frac / k
    return Match((x + s / 2) / k, (y + s / 2) / k, side, score)


def crop_square(img: Image.Image, m: Match, scale: float = 1.0) -> Image.Image:
    w, h = img.size
    side = min(m.side * scale, w, h)
    left = min(max(m.cx - side / 2, 0), w - side)
    top = min(max(m.cy - side / 2, 0), h - side)
    return img.crop((round(left), round(top), round(left + side), round(top + side))).resize(
        (SIZE, SIZE), Image.LANCZOS)


def circle(square_src: Image.Image) -> Image.Image:
    ss = 4
    mask = Image.new("L", (SIZE * ss, SIZE * ss), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, SIZE * ss - 1, SIZE * ss - 1), fill=255)
    out = square_src.convert("RGBA")
    out.putalpha(mask.resize((SIZE, SIZE), Image.LANCZOS))
    return out


def resolve(cid: str, alias: str, version: str, manual: dict) -> tuple[str, Image.Image, Match]:
    """The old icon's framing, from the largest splash that holds the same art."""
    icon_img = Image.open(fetch(f"{DDRAGON}/{version}/img/champion/{alias}.png",
                                CACHE / "icon" / f"{version}-{alias}.png")).convert("RGB")
    dd = Image.open(fetch(f"{DDRAGON}/img/champion/splash/{alias}_0.jpg",
                          CACHE / "splash" / f"{alias}_0.jpg")).convert("RGB")
    sources: dict[str, Image.Image] = {}
    hd_path = HD_DIR / f"{cid}.jpg"
    if hd_path.exists():
        sources["hd"] = Image.open(hd_path).convert("RGB")
    uni_path = universe_splash(alias)
    if uni_path is not None:
        uni = Image.open(uni_path).convert("RGB")
        if uni.width > dd.width:
            sources["universe"] = uni
    sources["ddragon"] = dd

    if cid in manual:  # hand crop: side is the visible area, widen it by the site zoom
        c = manual[cid]
        name = c["source"]
        if name == "tile":
            img = Image.open(fetch(cdragon_tile_url(cid), CACHE / "tile" / f"{cid}000.jpg")).convert("RGB")
        else:
            img = sources[name]
        w, h = img.size
        return f"{name}+manual", img, Match(c["cx"] * w, c["cy"] * h, c["side"] * w * OLD_ZOOM, 1.0)

    icon = to_bgr(icon_img)
    best = None
    for name, img in sources.items():
        m = locate(icon, to_bgr(img), ICON_INSET)
        if m.score >= GOOD_SCORE:
            return name, img, m
        if best is None or m.score > best[2].score:
            best = (name, img, m)
    if best[2].score >= MIN_SCORE:
        return best
    # Icon art is in no splash and nobody cropped it: keep the old icon itself.
    n = icon_img.width
    return "icon", icon_img, Match(n / 2, n / 2, n, best[2].score)


def build_one(cid: str, info: dict, version: str, force: bool, manual: dict) -> dict:
    alias = info["alias"]
    sq_path = OUT_DIR / f"{cid}.webp"
    ci_path = OUT_DIR / f"{cid}-circle.webp"
    source, img, m = resolve(cid, alias, version, manual)
    if force or not sq_path.exists() or not ci_path.exists():
        crop_square(img, m).save(sq_path, "WEBP", quality=88, method=6)
        circle(crop_square(img, m, CIRCLE_SCALE)).save(ci_path, "WEBP", quality=88, method=6)
    return {"alias": alias, "source": source, "score": round(m.score, 3),
            "src_side": round(m.side), "ok": m.score >= MIN_SCORE,
            "hires": m.side >= MIN_HIRES}


def main() -> int:
    global OUT_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated champion ids")
    ap.add_argument("--force", action="store_true", help="overwrite existing icons")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help=f"output dir (default {OUT_DIR})")
    args = ap.parse_args()
    OUT_DIR = args.out
    champs = json.loads(TIER.read_text(encoding="utf-8"))["champs"]
    version = next(iter(champs.values()))["image"].split("/cdn/")[1].split("/")[0]
    only = set(args.only.split(",")) if args.only else None
    manual = json.loads(CROPS.read_text(encoding="utf-8")) if CROPS.exists() else {}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest = {k: v for k, v in manifest.items() if k in champs}
    for cid in sorted(champs, key=int):
        if only and cid not in only:
            continue
        row = build_one(cid, champs[cid], version, args.force, manual)
        manifest[cid] = row
        flags = ("" if row["ok"] else "  LOW-SCORE") + ("" if row["hires"] else "  LOW-RES")
        print(f"{cid:>4} {row['alias']:<14} {row['source']:<12} {row['score']:.3f} "
              f"side={row['src_side']}{flags}", flush=True)
    manifest_path.write_text(json.dumps(dict(sorted(manifest.items(), key=lambda kv: int(kv[0]))),
                                        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    bad = [k for k, v in manifest.items() if not (v["ok"] and v["hires"])]
    print(f"{len(manifest)} champions, {len(bad)} flagged: {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
