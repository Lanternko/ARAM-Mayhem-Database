"""Tier-list site rendering (split from build_tier_list.py): payload assembly,
HTML shell, --shell-only fast preview, OG/favicon images, icon localization."""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import tierlist_engine as _eng  # noqa: E402,F401
globals().update({_k: _v for _k, _v in vars(_eng).items() if not _k.startswith('__')})



def render_analytics_tags(
    *,
    cloudflare_token: str = "",
    ga_measurement_id: str = "",
) -> list[str]:
    tags: list[str] = []
    cloudflare_token = cloudflare_token.strip()
    ga_measurement_id = ga_measurement_id.strip()

    if cloudflare_token:
        cf_config = html.escape(json.dumps({"token": cloudflare_token}), quote=True)
        tags.append(
            "<script defer src='https://static.cloudflareinsights.com/beacon.min.js' "
            f"data-cf-beacon='{cf_config}'></script>"
        )

    if ga_measurement_id:
        ga_id = html.escape(ga_measurement_id, quote=True)
        ga_id_js = json.dumps(ga_measurement_id)
        tags.append(
            f"<script async src='https://www.googletagmanager.com/gtag/js?id={ga_id}'></script>"
            "<script>"
            "window.dataLayer=window.dataLayer||[];"
            "function gtag(){dataLayer.push(arguments);}"
            "gtag('js',new Date());"
            f"gtag('config',{ga_id_js});"
            "</script>"
        )

    return tags

def _load_font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/msjhbd.ttc" if bold else "C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/NotoSansTC-Bold.otf" if bold else "C:/Windows/Fonts/NotoSansTC-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()

def _draw_text_fit(draw, xy: tuple[int, int], text: str, font, fill: str, max_width: int) -> None:
    # Pillow can hang measuring some CJK fonts on Windows, so keep this
    # deliberately simple for the fixed-size OG canvas.
    char_budget = max(8, max_width // 20)
    if len(text) > char_budget:
        text = text[: char_budget - 3].rstrip() + "..."
    draw.text(xy, text, font=font, fill=fill)

def _draw_prismatic_frame(img, box: tuple[int, int, int, int], radius: int) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    x1, y1, x2, y2 = box
    border_w = 14
    stops = [
        (0.00, (216, 184, 255)),
        (0.28, (188, 214, 255)),
        (0.56, (255, 213, 236)),
        (0.82, (231, 213, 255)),
        (1.00, (216, 184, 255)),
    ]

    def sample(t: float) -> tuple[int, int, int, int]:
        for idx in range(len(stops) - 1):
            left_t, left = stops[idx]
            right_t, right = stops[idx + 1]
            if t <= right_t:
                local = 0.0 if right_t == left_t else (t - left_t) / (right_t - left_t)
                rgb = tuple(int(left[c] + (right[c] - left[c]) * local) for c in range(3))
                return (*rgb, 255)
        return (*stops[-1][1], 255)

    ring_mask = Image.new("L", img.size, 0)
    ring_draw = ImageDraw.Draw(ring_mask)
    ring_draw.rounded_rectangle(box, radius=radius, fill=255)
    ring_draw.rounded_rectangle(
        (x1 + border_w, y1 + border_w, x2 - border_w, y2 - border_w),
        radius=radius - border_w,
        fill=0,
    )

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.rounded_rectangle(
        (x1 - 5, y1 - 5, x2 + 5, y2 + 5),
        radius=radius + 5,
        outline=(216, 184, 255, 140),
        width=9,
    )
    glow_draw.rounded_rectangle(
        (x1 - 10, y1 - 10, x2 + 10, y2 + 10),
        radius=radius + 10,
        outline=(188, 214, 255, 80),
        width=7,
    )
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(7)))

    gradient = Image.new("RGBA", img.size, (0, 0, 0, 0))
    px = gradient.load()
    denom = max(1, (x2 - x1) + (y2 - y1))
    for y in range(y1, y2 + 1):
        for x in range(x1, x2 + 1):
            if ring_mask.getpixel((x, y)):
                px[x, y] = sample(((x - x1) + (y - y1)) / denom)
    img.alpha_composite(gradient)

    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (x1 + border_w + 2, y1 + border_w + 2, x2 - border_w - 2, y2 - border_w - 2),
        radius=radius - border_w - 2,
        outline="#090c12",
        width=4,
    )

def write_og_image(
    out_path: Path,
    records: list[dict],
    champ_meta: dict[int, dict],
    *,
    queue_id: int,
    patch_prefix: str | None,
    total_games: int,
) -> None:
    """Write a square top-champion thumbnail for Open Graph cards."""
    from PIL import Image, ImageDraw

    top_record = records[0] if records else None
    top_meta = champ_meta.get(top_record["champion_id"]) if top_record else None
    top_wr = float(top_record.get("bayes_wr", 0.0)) if top_record else 0.0

    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    badge_font = _load_font(58, bold=True)

    card_x, card_y, card_size = 58, 58, 396
    frame_box = (card_x - 24, card_y - 24, card_x + card_size + 24, card_y + card_size + 24)
    draw.rounded_rectangle(frame_box, radius=36, fill="#080a10")
    _draw_prismatic_frame(img, frame_box, 36)
    if top_meta and top_meta.get("image"):
        try:
            resp = httpx.get(top_meta["image"], timeout=5)
            resp.raise_for_status()
            icon = Image.open(BytesIO(resp.content)).convert("RGB").resize((card_size, card_size))
            mask = Image.new("L", (card_size, card_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle((0, 0, card_size, card_size), radius=24, fill=255)
            img.paste(icon, (card_x, card_y), mask)
        except Exception:
            draw.rounded_rectangle((card_x, card_y, card_x + card_size, card_y + card_size), radius=24, fill="#242b3a")
    else:
        draw.rounded_rectangle(
            (card_x, card_y, card_x + card_size, card_y + card_size),
            radius=24,
            fill="#242b3a",
        )
    badge_text = f"{top_wr * 100:.1f}%"
    draw.rounded_rectangle((card_x, card_y + card_size - 102, card_x + 190, card_y + card_size), radius=22, fill="#0d111a")
    draw.text((card_x + 22, card_y + card_size - 86), badge_text, font=badge_font, fill="#f8fbff")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG", optimize=True)

def write_favicon_svg(out_path: Path) -> None:
    """Write a compact site favicon inspired by the Mayhem prismatic dice mark."""
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'>
  <defs>
    <linearGradient id='bg' x1='32' y1='24' x2='224' y2='232' gradientUnits='userSpaceOnUse'>
      <stop offset='0' stop-color='#0d1122'/>
      <stop offset='0.55' stop-color='#090d1d'/>
      <stop offset='1' stop-color='#05070f'/>
    </linearGradient>
    <linearGradient id='sheen' x1='58' y1='62' x2='194' y2='192' gradientUnits='userSpaceOnUse'>
      <stop offset='0' stop-color='#fbf7ff'/>
      <stop offset='0.22' stop-color='#8ef2ff'/>
      <stop offset='0.48' stop-color='#f5b6ff'/>
      <stop offset='0.72' stop-color='#ffe8ad'/>
      <stop offset='1' stop-color='#7ddfff'/>
    </linearGradient>
    <linearGradient id='orbit' x1='30' y1='188' x2='228' y2='110' gradientUnits='userSpaceOnUse'>
      <stop offset='0' stop-color='#f180ff'/>
      <stop offset='0.45' stop-color='#fff7ef'/>
      <stop offset='1' stop-color='#9f78ff'/>
    </linearGradient>
    <filter id='softGlow' x='-40%' y='-40%' width='180%' height='180%'>
      <feGaussianBlur stdDeviation='4' result='blur'/>
      <feMerge>
        <feMergeNode in='blur'/>
        <feMergeNode in='SourceGraphic'/>
      </feMerge>
    </filter>
  </defs>
  <rect x='8' y='8' width='240' height='240' rx='34' fill='url(#bg)'/>
  <rect x='8' y='8' width='240' height='240' rx='34' fill='none' stroke='rgba(255,255,255,0.18)' stroke-width='3'/>
  <g filter='url(#softGlow)' stroke='url(#sheen)' stroke-width='3.5' stroke-linejoin='round'>
    <path d='M128 56 69 94l59 34 59-34-59-38Z' fill='rgba(255,248,255,0.88)'/>
    <path d='M69 94v69l59 35v-70L69 94Z' fill='rgba(232,220,255,0.78)'/>
    <path d='M187 94v69l-59 35v-70l59-34Z' fill='rgba(244,205,255,0.8)'/>
  </g>
  <g fill='#090d1d'>
    <ellipse cx='128' cy='101' rx='11' ry='8'/>
    <ellipse cx='91' cy='122' rx='10' ry='14' transform='rotate(-24 91 122)'/>
    <ellipse cx='108' cy='164' rx='10' ry='14' transform='rotate(-24 108 164)'/>
    <ellipse cx='153' cy='142' rx='10' ry='14' transform='rotate(24 153 142)'/>
    <ellipse cx='171' cy='122' rx='10' ry='14' transform='rotate(24 171 122)'/>
  </g>
  <path d='M31 181c26 21 59 29 95 27 38-2 72-15 101-49' fill='none' stroke='#05070f' stroke-width='18' stroke-linecap='round'/>
  <path d='M27 177c26 21 59 29 95 27 38-2 72-15 101-49' fill='none' stroke='url(#orbit)' stroke-width='11' stroke-linecap='round' filter='url(#softGlow)'/>
  <path d='M191 64l5 14 14 5-14 5-5 14-5-14-14-5 14-5 5-14Z' fill='#fff3d5' filter='url(#softGlow)'/>
</svg>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8")

def favicon_asset_version() -> str:
    """Use icon-source or generator mtime so browser cache updates on asset tweaks."""
    candidates = [Path(__file__)]
    if SITE_ICON_SOURCE.exists():
        candidates.append(SITE_ICON_SOURCE)
    existing = [path for path in candidates if path.exists()]
    if existing:
        latest = max(path.stat().st_mtime for path in existing)
        stamp = _dt.datetime.fromtimestamp(latest)
        return stamp.strftime("%Y%m%d%H%M%S")
    return (_dt.date.today().isoformat()).replace("-", "")

def write_favicon_assets(out_dir: Path, source_path: Path = SITE_ICON_SOURCE) -> list[Path]:
    """Generate favicon PNG/ICO assets by directly downscaling the checked-in icon."""
    from PIL import Image, ImageChops, ImageDraw

    if not source_path.exists():
        return []

    img_master = Image.open(source_path).convert("RGBA")
    source_has_alpha = img_master.getchannel("A").getextrema()[0] < 255

    def _resized(img_rgba: "Image.Image", size: tuple[int, int]) -> "Image.Image":
        resized = img_rgba.resize(size, Image.LANCZOS)
        if source_has_alpha:
            return resized
        radius = max(4, round(min(size) * 0.22))
        mask = Image.new("L", size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
        alpha = resized.getchannel("A")
        resized.putalpha(ImageChops.multiply(alpha, mask))
        return resized

    out_dir.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    raster_targets = {
        "mayhem-single-die-icon.png": (180, 180),
        "mayhem-tab-icon.png": (180, 180),
        "favicon-32.png": (32, 32),
        "apple-touch-icon.png": (180, 180),
    }
    for name, size in raster_targets.items():
        target = out_dir / name
        resized = _resized(img_master, size)
        resized.save(target, "PNG", optimize=True)
        outputs.append(target)

    ico_path = out_dir / "favicon.ico"
    ico_master = _resized(img_master, (256, 256))
    ico_master.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    outputs.append(ico_path)
    return outputs


def write_role_definitions_json(
    out_path: Path,
    *,
    champ_meta: dict[int, dict] | None = None,
    data_dragon_version: str | None = None,
    patch_prefix: str | None = None,
) -> None:
    payload = role_definitions_payload()
    payload["generated_at"] = _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload["data_dragon_version"] = data_dragon_version
    payload["patch_prefix"] = patch_prefix
    if champ_meta:
        current_roles: dict[str, dict[str, object]] = {}
        secondary_roles: dict[str, dict[str, object]] = {}
        for meta in champ_meta.values():
            alias = str(meta.get("alias") or "")
            if not alias:
                continue
            tags = list(meta.get("tags") or [])
            primary = str(tags[0]) if tags else ""
            secondary = str(tags[1]) if len(tags) > 1 else ""
            role_meta = meta.get("role_meta") or {}
            current_roles[alias] = {
                "primary": primary,
                "secondary": secondary,
                "tags": tags,
            }
            if secondary:
                secondary_roles[alias] = {
                    "role": secondary,
                    "meta": role_meta.get(secondary, {}),
                }
        payload["current_roles"] = dict(sorted(current_roles.items()))
        payload["secondary_roles"] = dict(sorted(secondary_roles.items()))
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

def localize_cdragon_icons(
    payload: dict,
    assets_dir: Path,
    *,
    rel_prefix: str = "assets/icons",
) -> int:
    """Download every CommunityDragon icon in `payload` and rewrite its URL to a
    site-relative path under `assets_dir`.

    `raw.communitydragon.org` is slow / unreachable on some networks, so any icon
    that has no Data Dragon equivalent (Mayhem-only items, every augment, set /
    particle icons) would otherwise hang forever. Self-hosting them removes the
    runtime dependency. Standard item icons already point at Data Dragon and are
    left untouched. Files that already exist on disk are not re-downloaded, and a
    failed download keeps the remote URL as a fallback.
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, str] = {}
    downloaded = 0

    def localize(url: str) -> str:
        nonlocal downloaded
        if url in resolved:
            return resolved[url]
        name = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if not name:
            resolved[url] = url
            return url
        dest = assets_dir / name
        rel = f"{rel_prefix}/{name}"
        if not dest.exists():
            try:
                r = httpx.get(url, timeout=30, follow_redirects=True)
                r.raise_for_status()
                dest.write_bytes(r.content)
                downloaded += 1
            except Exception as exc:  # keep remote URL as a fallback
                click.echo(f"[tierlist] WARN: failed to self-host {url}: {exc}")
                resolved[url] = url
                return url
        resolved[url] = rel
        return rel

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and value.startswith("https://raw.communitydragon.org"):
                    node[key] = localize(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str) and value.startswith("https://raw.communitydragon.org"):
                    node[idx] = localize(value)
                else:
                    walk(value)

    walk(payload)
    click.echo(
        f"[tierlist] self-hosted {len(resolved)} CommunityDragon icons "
        f"({downloaded} newly downloaded) -> {assets_dir}"
    )
    return downloaded

def _read_site_template(name: str) -> str:
    """Load a frontend template (site.css / site.js) shipped beside this script.

    The site CSS/JS used to live as ~6.7k lines of inline string literals inside
    render_html.  They now live in scripts/templates/ for editability (syntax
    highlighting + linting) and to shrink this file.  Returned verbatim and
    interpolated exactly as the old inline literals were, so emitted HTML is
    byte-identical.  Resolved relative to THIS file so it works regardless of the
    caller's CWD (e.g. the static_publish.py subprocess invoked from repo root).
    """
    return (Path(__file__).resolve().parent / "templates" / name).read_text(encoding="utf-8")


def _site_base_href(site_url: str) -> str:
    """Absolute site root ending in / for <base href>, or '' if unusable."""
    raw = (site_url or "").strip()
    if not raw:
        return ""
    # Accept "https://arammeta.com" or ".../" or a path prefix.
    if "://" not in raw:
        return "/" if raw == "/" else (raw.rstrip("/") + "/")
    # scheme://host[/path]
    try:
        from urllib.parse import urlparse
        p = urlparse(raw)
        if not p.scheme or not p.netloc:
            return "/"
        prefix = (p.path or "/").rstrip("/")
        if not prefix:
            prefix = ""
        return f"{p.scheme}://{p.netloc}{prefix}/"
    except Exception:
        return raw if raw.endswith("/") else raw + "/"


def discover_column_article_ids(site_js: str | None = None) -> list[str]:
    """Article slugs from the ARTICLES array in site.js (shareable /column/<id>)."""
    text = site_js if site_js is not None else _read_site_template("site.js")
    m = re.search(r"const ARTICLES\s*=\s*\[(.*?)\n\s*\];", text, re.S)
    if not m:
        return []
    # Only top-level `id: 'slug'` entries inside the array body.
    return re.findall(r"(?m)^\s*id:\s*'([a-z0-9][a-z0-9-]*)'\s*,?\s*$", m.group(1))


def _spa_deep_link_stub(
    *,
    site_url: str = "",
    og_image: str = "",
    canonical_path: str = "/",
    title: str = "arammeta",
    description: str = "",
) -> str:
    """Tiny GH Pages shell: stash path → bounce to / so the real SPA can boot.

    Avoids copying the full ~500KB index.html to every clean path (repo bloat)
    while still giving shareable URLs HTTP 200 + basic OG tags for crawlers.
    The SPA restores `sessionStorage['aram-spa-path']` on boot.
    """
    base = _site_base_href(site_url) or "/"
    origin = base.rstrip("/")
    path = canonical_path if canonical_path.startswith("/") else "/" + canonical_path
    canonical = (origin + path) if origin.startswith("http") else path
    desc = description or title
    og_img = og_image or ((origin + "/og-image.png") if origin.startswith("http") else "")
    esc = html.escape
    og_bits = [
        f"<meta property='og:type' content='website'>",
        f"<meta property='og:title' content=\"{esc(title, quote=True)}\">",
        f"<meta property='og:description' content=\"{esc(desc, quote=True)}\">",
        f"<meta property='og:url' content='{esc(canonical, quote=True)}'>",
        f"<meta name='twitter:card' content='summary'>",
        f"<meta name='twitter:title' content=\"{esc(title, quote=True)}\">",
        f"<meta name='twitter:description' content=\"{esc(desc, quote=True)}\">",
    ]
    if og_img:
        og_bits.append(f"<meta property='og:image' content='{esc(og_img, quote=True)}'>")
        og_bits.append(f"<meta name='twitter:image' content='{esc(og_img, quote=True)}'>")
    return (
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title>"
        f"<link rel='canonical' href='{esc(canonical, quote=True)}'>"
        + "".join(og_bits)
        + "<script>"
        "try{sessionStorage.setItem('aram-spa-path',"
        "location.pathname+location.search+location.hash)}catch(e){}"
        "location.replace('/');"
        "</script>"
        f"<meta http-equiv='refresh' content='0;url=/'>"
        f"<noscript><a href='/'>arammeta</a></noscript>"
        "</head><body></body></html>\n"
    )


def write_spa_path_shells(
    index_path: Path,
    *,
    site_url: str = "",
    og_image: str = "",
) -> list[Path]:
    """Write lightweight deep-link stubs + 404.html for clean path URLs.

    History API routes like /column/<id> need *something* on GH Pages (no
    rewrites).  Stubs return HTTP 200, carry basic OG tags for shares, then
    bounce to / where site.js restores the path from sessionStorage.
    """
    index_path = Path(index_path)
    if not index_path.is_file():
        return []
    root = index_path.parent

    # Best-effort OG image from the main shell when caller did not pass one.
    if not og_image and site_url:
        og_image = _site_base_href(site_url).rstrip("/") + "/og-image.png"

    # Pull article titles from site.js for slightly better share cards.
    site_js = _read_site_template("site.js")
    article_titles: dict[str, str] = {}
    for aid in discover_column_article_ids(site_js):
        # Prefer zh title next to this id block.
        m = re.search(
            rf"id:\s*'{re.escape(aid)}'.*?title_zh:\s*'((?:\\'|[^'])*)'",
            site_js,
            re.S,
        )
        if m:
            article_titles[aid] = m.group(1).replace("\\'", "'")

    route_specs: list[tuple[Path, str, str, str]] = [
        # (file, canonical_path, title, description)
        (root / "404.html", "/", "arammeta", ""),
        (root / "augments" / "index.html", "/augments", "增幅榜 · arammeta", "ARAM 大亂鬥增幅勝率榜"),
        (root / "changes" / "index.html", "/changes", "版本變動 · arammeta", "版本勝率變動"),
        (root / "column" / "index.html", "/column", "專欄 · arammeta", "資料背後的思考與玩法解析"),
    ]
    for article_id, title in article_titles.items():
        route_specs.append(
            (
                root / "column" / article_id / "index.html",
                f"/column/{article_id}",
                f"{title} · arammeta",
                title,
            )
        )

    written: list[Path] = []
    for dest, cpath, title, desc in route_specs:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            _spa_deep_link_stub(
                site_url=site_url,
                og_image=og_image,
                canonical_path=cpath,
                title=title,
                description=desc,
            ),
            encoding="utf-8",
        )
        written.append(dest)
    return written


def _dedupe_item_objects(payload: dict) -> None:
    """Hoist repeated item identities into a top-level lookup to shrink the payload.

    Item recommendation rows embed the same item identity ({name, name_zh,
    name_en, icon}) thousands of times — ~26k occurrences of ~130 distinct items,
    several MB of pure duplication.  We record each distinct item once in
    payload['itemLut'] ({id: {z: name_zh, e: name_en}}), strip the name/icon
    fields off every embedded copy (keeping its id and any per-row stats
    untouched) and tag it with "ic": 1.  The frontend's rehydrateItems() restores
    name + icon (from id + the data-dragon version in payload['ddv']) on load, so
    every existing render path is unchanged and the rendered site is identical.
    No-op when there are no ddragon item icons (e.g. an already-deduped payload).
    """
    item_icon = re.compile(r"/cdn/([0-9.]+)/img/item/(\d+)\.png")
    lut: dict[str, dict] = {}
    version = ""

    def visit(node):
        nonlocal version
        if isinstance(node, dict):
            icon = node.get("icon")
            if isinstance(icon, str) and "id" in node and item_icon.search(icon):
                version = version or item_icon.search(icon).group(1)
                iid = str(node["id"])
                if iid not in lut:
                    entry = {"e": node.get("name_en") or node.get("name") or ""}
                    zh = node.get("name_zh") or node.get("name") or ""
                    if zh:
                        entry["z"] = zh
                    lut[iid] = entry
                for k in ("name", "name_zh", "name_en", "icon"):
                    node.pop(k, None)
                node["ic"] = 1
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        visit(v)
                return
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(payload.get("champs"))
    if lut:
        # Attach gold cost + cleaned item text so the site can show rich hover
        # cards without re-embedding descriptions on every build row.
        try:
            item_meta = load_item_metadata(
                cache_dir=Path("data/cache"),
                ddragon_version=version or None,
            )
        except Exception:
            item_meta = {}
        for iid, entry in lut.items():
            try:
                meta = item_meta.get(int(iid)) or {}
            except (TypeError, ValueError):
                meta = {}
            if not meta:
                continue
            price = int(meta.get("price_total") or 0)
            if price > 0:
                entry["p"] = price
            dz = str(meta.get("desc_zh") or "").strip()
            de = str(meta.get("desc_en") or "").strip()
            if dz:
                entry["dz"] = dz
            if de:
                entry["de"] = de
        payload["itemLut"] = lut
        payload["ddv"] = version


def render_html(
    records: list[dict],
    champ_meta: dict[int, dict],
    champ_profiles: dict[int, dict[str, object]],
    champ_picks: dict[int, dict],
    champ_sets: dict[int, dict],
    champ_item_builds: dict[int, dict],
    champ_single_items: dict[int, dict],
    champ_boot_items: dict[int, dict],
    champ_spell_items: dict[int, dict],
    champ_item_clusters: dict[int, dict],
    champ_augment_types: dict[int, dict],
    champ_synergy: dict[int, list[dict]],
    aug_meta: dict[int, dict],
    patch_changes: dict[str, object] | None,
    *,
    new_aug_ids: set[int] | frozenset[int] = frozenset(),
    queue_id: int,
    patch_prefix: str | None,
    ddragon_version: str,
    total_games: int,
    min_games_per_pair: int,
    min_synergy_games: int,
    site_url: str = "",
    og_image: str = "",
    build_date: str = "",
    cloudflare_analytics_token: str = "",
    ga_measurement_id: str = "",
    payload_out_path: Path | None = None,
    payload_url: str = "",
    icon_assets_dir: Path | None = None,
    aug_global: dict[int, dict] | None = None,
) -> str:
    # Group champions by tier
    by_tier: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for r in records:
        tier = assign_tier(r["bayes_wr"])
        meta = champ_meta.get(r["champion_id"])
        if meta is None:
            continue
        by_tier[tier].append({**r, **meta})

    header_title, queue_label = _queue_copy(queue_id)
    # Site brand is "arammeta" (the arammeta.com identity); override the queue
    # copy so the header brand, <title> and share cards all read "arammeta".
    # queue_label still carries the descriptive queue string used elsewhere.
    header_title = "arammeta"
    header_title_en = "arammeta"
    display_patch = display_patch_prefix(patch_prefix)
    patch_label = f"patch {display_patch}" if display_patch else "all patches"

    # Build the JS data payload. Keep it slim: only champs we render + their
    # picked augments / teammate synergy rows + the augment metadata for ids
    # that actually appear.
    used_aug_ids: set[int] = set()
    js_champs: dict[str, dict] = {}

    # Per-champion skill-scaling ("operation coefficient"): WR(high-skill lobbies) - WR(low-skill).
    # Decoupled build-time artifact from build_skill_scaling_rating.py; absent -> field omitted.
    skill_scaling_by_cid: dict[int, dict] = {}
    _ss_path = Path("data/cache/champ_skill_scaling.json")
    if _ss_path.exists():
        try:
            _ss_raw = json.loads(_ss_path.read_text(encoding="utf-8"))
            for _k, _v in (_ss_raw.get("champs") or {}).items():
                skill_scaling_by_cid[int(_k)] = _v
            click.echo(f"[tierlist] loaded skill-scaling for {len(skill_scaling_by_cid)} champions")
        except (ValueError, OSError):
            skill_scaling_by_cid = {}

    def _pack(r: dict) -> dict:
        # Display rule: show a number the raw sample can support.  We clamp the
        # shrunk posterior mean into the raw 95% Wilson CI, so a high-sample
        # pair (e.g. TF Echo Cast, n=573) is never dragged below its own CI
        # lower bound, while low-sample pairs still shrink toward baseline.
        # The shrunk value (rank_score / lcb_lift) is kept untouched for SORTING.
        games = int(r["games"])
        wins = int(r.get("wins", round(float(r.get("raw_wr", 0.0)) * games)))
        smoothed = float(r["smoothed_wr"])
        baseline = smoothed - float(r["lift"])  # baseline_wr, derived
        lo, hi = raw_wilson_bounds(wins, games)
        display_wr = min(max(smoothed, lo), hi)
        return {
            "id": r["augment_id"],
            "g": games,
            "wr": round(display_wr, 4),
            "rawWr": round(float(r.get("raw_wr", display_wr)), 4),
            "lift": round(display_wr - baseline, 4),
            "score": round(r.get("rank_score", r["lift"]), 4),
            "lcb": round(r.get("lcb_lift", r["lift"]), 4),
            "pick": round(r.get("pick_rate", 0.0), 4),
            "peerPick": round(r.get("peer_pick_rate", 0.0), 4),
            "pickLift": round(r.get("pick_lift", 0.0), 3),
        }

    def _pack_set(r: dict) -> dict:
        avg_value = float(r.get("avg_lift", r.get("global_lift", 0.0)) or 0.0)
        residual_value = float(r.get("residual", float(r.get("lift", 0.0) or 0.0) - avg_value) or 0.0)
        packed = {
            "name": r.get("set", r.get("name", r["slug"])),
            "name_zh": r.get("set_zh", r.get("name_zh", r.get("set", r.get("name", r["slug"])))),
            "name_en": r.get("set_en", r.get("name_en", r.get("set", r.get("name", r["slug"])))),
            "slug": r["slug"],
            "g": r["games"],
            "wr": round(r["smoothed_wr"], 4),
            "lift": round(r["lift"], 4),
            "avg": round(avg_value, 4),
            "res": round(residual_value, 4),
            "score": round(r.get("rank_score", r.get("lcb_residual", residual_value)), 4),
            "badScore": round(r.get("rank_bad_score", r.get("ucb_residual", residual_value)), 4),
            "pick": round(r.get("pick_rate", 0.0), 4),
            "globalPick": round(r.get("global_pick_rate", 0.0), 4),
            "pickLift": round(r.get("pick_lift", 0.0), 3),
            "pickCredit": round(r.get("pick_rate_credit", 0.0), 4),
            "peerGroup": r.get("peer_group", ""),
            "peerScope": r.get("peer_scope", ""),
        }
        if r.get("lane"):
            packed["lane"] = str(r["lane"])
        optional_float_fields = {
            "pair_lift": "pairLift",
            "single_lift": "singleLift",
            "global_lift": "globalLift",
            "core_pair_lift": "corePairLift",
            "core_single_lift": "coreSingleLift",
            "flex_single_lift": "flexSingleLift",
            "flex_stability": "flexStability",
            "core_lcb": "coreLcb",
        }
        for source_key, dest_key in optional_float_fields.items():
            if source_key in r:
                packed[dest_key] = round(float(r.get(source_key, 0.0)), 4)
        optional_int_fields = {
            "cluster_size": "routeSize",
            "pair_coverage": "pairCoverage",
            "core_pair_coverage": "corePairCoverage",
            "cluster_games": "clusterGames",
            "exact_games": "exactGames",
        }
        for source_key, dest_key in optional_int_fields.items():
            if source_key in r:
                packed[dest_key] = int(r.get(source_key, 0) or 0)
        if r.get("items"):
            packed["items"] = r["items"]
        return packed

    def _pack_core_group(grp: dict) -> dict:
        def _pack_option(o: dict) -> dict:
            item = dict(o.get("item") or {})
            item.update({
                "g": int(o.get("games", 0) or 0),
                "wr": round(float(o.get("smoothed_wr", 0.0) or 0.0), 4),
                "lift": round(float(o.get("lift", 0.0) or 0.0), 4),
                "lcb": round(float(o.get("core_lcb", 0.0) or 0.0), 4),
                "pick": round(float(o.get("pick_rate", 0.0) or 0.0), 4),
                "exactGames": int(o.get("exact_games", 0) or 0),
                "lane": str(o.get("lane", "") or ""),
            })
            return item
        return {
            "name": grp.get("name", grp.get("name_zh", "")),
            "name_zh": grp.get("name_zh", grp.get("name", "")),
            "name_en": grp.get("name_en", grp.get("name", "")),
            "slug": grp.get("slug", ""),
            "core": grp.get("core_items", []),
            "g": int(grp.get("games", 0) or 0),
            "wr": round(float(grp.get("smoothed_wr", 0.0) or 0.0), 4),
            "lift": round(float(grp.get("lift", 0.0) or 0.0), 4),
            "pick": round(float(grp.get("pick_rate", 0.0) or 0.0), 4),
            "options": [_pack_option(o) for o in grp.get("options", [])],
            "tail": grp.get("tail_items", []),
        }

    def _pack_comp(profile: dict[str, object]) -> dict:
        return {
            "phys": round(float(profile.get("physical_dpm") or 0.0), 3),
            "magic": round(float(profile.get("magic_dpm") or 0.0), 3),
            "true": round(float(profile.get("true_dpm") or 0.0), 3),
            "wave": round(float(profile.get("wave") or 0.0), 3),
            "cc": round(float(profile.get("cc") or 0.0), 3),
            "engage": round(float(profile.get("engage") or 0.0), 3),
            "damage": round(float(profile.get("damage_score") or 0.0), 3),
            "poke": round(float(profile.get("poke") or 0.0), 3),
            "sustain": round(float(profile.get("sustain") or 0.0), 3),
            "front": round(float(profile.get("front") or 0.0), 3),
        }

    def _pack_role_meta(role_meta: dict[str, dict[str, object]] | None) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for role, info in (role_meta or {}).items():
            out[role] = {
                "role": role,
                "slot": info.get("slot", ""),
                "source": info.get("source", ""),
                "wr": round(float(info.get("wr", 0.0) or 0.0), 4),
                "games": int(info.get("games", 0) or 0),
                "pick": (
                    round(float(info.get("pick_rate", 0.0) or 0.0), 4)
                    if info.get("pick_rate") is not None else None
                ),
                "lift": (
                    round(float(info.get("lift", 0.0) or 0.0), 4)
                    if info.get("lift") is not None else None
                ),
                "score": (
                    round(float(info.get("score", 0.0) or 0.0), 4)
                    if info.get("score") is not None else None
                ),
                "styleSlug": info.get("style_slug", ""),
                "styleName": info.get("style_name", ""),
                "styleNameZh": info.get("style_name_zh", ""),
                "styleNameEn": info.get("style_name_en", ""),
                "roleLabelZh": info.get("role_label_zh", role),
                "roleLabelEn": info.get("role_label_en", role),
            }
        return out

    def _add_search_terms(terms: list[str], *values: object) -> None:
        for value in values:
            if value is None:
                continue
            if isinstance(value, dict):
                _add_search_terms(terms, *value.values())
                continue
            if isinstance(value, (list, tuple, set)):
                _add_search_terms(terms, *value)
                continue
            text = str(value).strip()
            if text:
                terms.append(text)

    def _add_named_rows_for_search(terms: list[str], rows: list[dict]) -> None:
        for row in rows:
            _add_search_terms(
                terms,
                row.get("name"),
                row.get("name_zh"),
                row.get("name_en"),
                row.get("set"),
                row.get("set_zh"),
                row.get("set_en"),
                row.get("slug"),
            )
            for item in row.get("items") or []:
                _add_search_terms(
                    terms,
                    item.get("name"),
                    item.get("name_zh"),
                    item.get("name_en"),
                    item.get("id"),
                )

    def _champ_search_blob(cid: int, display_name: str, meta: dict, tags: list[str]) -> str:
        terms: list[str] = []
        _add_search_terms(
            terms,
            display_name,
            meta.get("name"),
            meta.get("name_zh"),
            meta.get("name_en"),
            meta.get("alias"),
            tags,
        )
        # NOTE: augment / item / set search terms used to be packed in here too
        # (~1 MB across all champs), but the client's enrichSearchIndexes() rebuilds
        # the full search blob from the loaded payload on init (see site.js) and
        # OVERWRITES this attribute — so server-rendering them was pure duplication
        # that only bloated index.html.  We now emit just the champion's own name
        # terms; the client fills in augment / item / set terms from the payload on
        # load.  Search behaves identically (production already relied on the client
        # rebuild).
        seen: set[str] = set()
        unique_terms: list[str] = []
        for term in terms:
            normalized = term.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_terms.append(normalized)
        return " ".join(unique_terms)

    visible_cids = [int(r["champion_id"]) for r in records]
    visible_cid_set = set(visible_cids)
    # Champion-level overall meta win rate, so downstream consumers (the
    # recommender's shared-bench copy) can show a real, cell-independent win
    # rate instead of a baseline-shifted per-cell estimate.  bayes_wr matches
    # the public tier-list headline number; raw_wr / games are kept alongside
    # for the empirical rate and sample size.
    champ_stat_by_cid = {int(r["champion_id"]): r for r in records}
    for cid in visible_cids:
        meta = champ_meta.get(cid)
        if meta is None:
            continue
        picks = champ_picks.get(cid, {"top": {}, "bot": {}})
        top_buckets = {}
        bot_buckets = {}
        for rarity in RARITY_ORDER:
            top_rows = picks["top"].get(rarity, [])
            bot_rows = picks["bot"].get(rarity, [])
            for r in top_rows + bot_rows:
                used_aug_ids.add(r["augment_id"])
            top_buckets[rarity] = [_pack(r) for r in top_rows]
            bot_buckets[rarity] = [_pack(r) for r in bot_rows]
        pairs = [
            {
                "id": row["teammate_id"],
                "g": row["games"],
                "wr": round(row["raw_wr"], 4),
                "expected": round(row["expected_wr"], 4),
                "lift": round(row["lift"], 4),
                "z": round(row["z_score"], 3),
            }
            for row in champ_synergy.get(cid, [])
            if row["teammate_id"] in visible_cid_set
        ]
        js_champs[str(cid)] = {
            "name": meta["name"],
            "name_zh": meta.get("name_zh", meta["name"]),
            "name_en": meta.get("name_en", meta.get("alias", meta["name"])),
            "alias": meta.get("alias", ""),
            "image": meta.get("image", ""),
            "tags": meta.get("tags") or [],
            "top": top_buckets,
            "bot": bot_buckets,
            "sets": {
                "top": [_pack_set(r) for r in champ_sets.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_sets.get(cid, {}).get("bot", [])],
            },
            "items": {
                "top": [_pack_set(r) for r in champ_item_builds.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_item_builds.get(cid, {}).get("bot", [])],
            },
            "singleItems": {
                "top": [_pack_set(r) for r in champ_single_items.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_single_items.get(cid, {}).get("bot", [])],
                "popularBad": [_pack_set(r) for r in champ_single_items.get(cid, {}).get("popular_bad", [])],
            },
            "boots": {
                "top": [_pack_set(r) for r in champ_boot_items.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_boot_items.get(cid, {}).get("bot", [])],
            },
            "spells": {
                "top": [_pack_set(r) for r in champ_spell_items.get(cid, {}).get("top", [])],
            },
            "itemClusters": {
                "groups": [_pack_core_group(grp) for grp in champ_item_clusters.get(cid, {}).get("groups", [])],
            },
            "augTypes": {
                "top": [_pack_set(r) for r in champ_augment_types.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_augment_types.get(cid, {}).get("bot", [])],
            },
            "pairs": pairs,
            "comp": _pack_comp(champ_profiles.get(cid, {})),
            "skillScaling": skill_scaling_by_cid.get(cid),
            "roleMeta": _pack_role_meta(meta.get("role_meta")),
            "wr": round(float(champ_stat_by_cid.get(cid, {}).get("bayes_wr", 0.0) or 0.0), 4),
            "rawWr": round(float(champ_stat_by_cid.get(cid, {}).get("raw_wr", 0.0) or 0.0), 4),
            "g": int(champ_stat_by_cid.get(cid, {}).get("games", 0) or 0),
        }
    aug_cat_overrides = load_augment_category_overrides()
    if aug_cat_overrides:
        click.echo(
            f"[tierlist] applied {len(aug_cat_overrides)} augment category overrides"
        )
    # Augments that carry a global win-rate (the 增幅榜 rollup) must ship their
    # metadata too, even if no champion ranked them into a top/bot pick bucket.
    if aug_global:
        used_aug_ids |= {aid for aid in aug_global if aid in aug_meta}
    js_augs = {
        str(aid): {
            "name": aug_meta[aid]["name"],
            "name_zh": aug_meta[aid].get("name_zh", aug_meta[aid]["name"]),
            "name_en": aug_meta[aid].get("name_en", aug_meta[aid]["name"]),
            "icon": aug_meta[aid]["icon"],
            "rarity": aug_meta[aid].get("rarity", ""),
            "desc": aug_meta[aid].get("desc", ""),
            "desc_zh": aug_meta[aid].get("desc_zh", aug_meta[aid].get("desc", "")),
            "desc_en": aug_meta[aid].get("desc_en", ""),
            "set": aug_meta[aid].get("set", ""),
            "set_zh": aug_meta[aid].get("set_zh", aug_meta[aid].get("set", "")),
            "set_en": aug_meta[aid].get("set_en", aug_meta[aid].get("set", "")),
            "setSlug": aug_meta[aid].get("setSlug", ""),
            "sets": aug_meta[aid].get("sets", []),
            "displayTags": aug_meta[aid].get("displayTags", []),
            "cats": resolve_augment_categories(
                aid, aug_meta[aid], new_aug_ids, aug_cat_overrides
            ),
        }
        for aid in used_aug_ids
        if aid in aug_meta
    }
    # Merge the global per-augment win-rate / pick share into the augment payload
    # the frontend already ships.  The 增幅榜 tier itself is computed client-side
    # (within-rarity percentile of wr) so it can be tuned without a rebuild.
    for _aid, _gs in (aug_global or {}).items():
        _key = str(_aid)
        if _key not in js_augs:
            continue
        js_augs[_key].update({
            "wr": round(float(_gs["wr"]), 4),
            "rawWr": round(float(_gs["rawWr"]), 4),
            "g": int(_gs["g"]),
            "lcb": round(float(_gs["lcb"]), 4),
            "lift": round(float(_gs["lift"]), 4),
            "pick": round(float(_gs["pick"]), 4),
            # current-patch games + fraction borrowed from the previous patch when
            # this patch was thin (see build_augment_global_stats); 0 = pure current.
            "curG": int(_gs.get("curG", _gs["g"])),
            "prevMix": round(float(_gs.get("prevMix", 0.0)), 3),
        })

    css = _read_site_template("site.css")

    payload = {
        "champs": js_champs,
        "augs": js_augs,
        "augCategories": {
            "order": list(AUGMENT_CATEGORY_ORDER),
            "labels": AUGMENT_CATEGORY_LABELS,
            "newPatch": display_patch or "",
        },
        "tiers": {
            "order": list(TIER_ORDER),
            "colors": {
                t: {"color": TIER_COLOR[t], "bg": TIER_LABEL_BG[t]}
                for t in TIER_ORDER
            },
        },
        "min_games_per_pair": min_games_per_pair,
        "min_synergy_games": min_synergy_games,
        "patchChanges": patch_changes or {},
        "recommendation_composition": {
            "weight": RECOMMENDATION_COMPOSITION_WEIGHT,
            "clamp": RECOMMENDATION_COMPOSITION_CLAMP,
            "lack_thresholds": COMPOSITION_LACK_THRESHOLDS,
            "table_weights": RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS,
            "tables": RECOMMENDATION_COMPOSITION_TABLES,
            "damage_mix": {
                "target_ad_share": RECOMMENDATION_DAMAGE_MIX_TARGET_AD,
                "weight": RECOMMENDATION_DAMAGE_MIX_WEIGHT,
                "clamp": RECOMMENDATION_DAMAGE_MIX_CLAMP,
            },
        },
    }
    if icon_assets_dir is not None:
        localize_cdragon_icons(payload, icon_assets_dir)
    _dedupe_item_objects(payload)
    payload_json = json.dumps(payload, ensure_ascii=False)
    if payload_out_path is not None:
        payload_out_path.parent.mkdir(parents=True, exist_ok=True)
        payload_out_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    og_patch_label = f"patch {display_patch}" if display_patch else "all patches"
    og_title = header_title  # share-card title = the brand
    og_desc = f"{og_patch_label}｜【英雄 x 增幅裝置勝率 · 組隊推薦】&#10;by 路燈"
    # Search snippet copy is deliberately separate from the share-card copy
    # above: Google drops keyword-less titles / symbol-heavy descriptions and
    # falls back to scraping nav text, so <title> + <meta description> carry
    # the terms players actually search while og:*/twitter:* keep the brand
    # card that Threads/Discord shares are known by.
    patch_zh = f"版本 {display_patch} " if display_patch else ""
    seo_title = f"ARAM 大亂鬥（Mayhem）英雄勝率 Tier List・增幅與裝備數據｜{header_title}"
    seo_desc = (
        f"基於 {total_games:,} 場台服 ARAM 大亂鬥（Mayhem）實戰對局的英雄勝率排行、"
        f"增幅勝率、出裝與組隊推薦，{patch_zh}持續更新。"
    )

    meta_lines: list[str] = []
    meta_lines.append("<meta charset='utf-8'>")
    meta_lines.append(
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    )
    meta_lines.append(f"<title>{seo_title}</title>")
    favicon_version = favicon_asset_version()
    meta_lines.append(
        f"<link rel='icon' type='image/png' href='mayhem-single-die-icon.png?v={favicon_version}'>"
    )
    meta_lines.append(
        f"<link rel='apple-touch-icon' href='apple-touch-icon.png?v={favicon_version}'>"
    )
    meta_lines.append(f"<meta name='description' content=\"{seo_desc}\">")
    if site_url:
        # <base> keeps relative assets (api/, assets/, favicons) resolving from the
        # site root when History API deep paths like /column/<id> are active.
        base_href = _site_base_href(site_url)
        if base_href:
            meta_lines.append(f"<base href='{html.escape(base_href, quote=True)}'>")
        meta_lines.append(f"<link rel='canonical' href='{site_url}'>")
        meta_lines.append(f"<meta property='og:url' content='{site_url}'>")
    meta_lines.append("<meta property='og:type' content='website'>")
    meta_lines.append(f"<meta property='og:title' content=\"{og_title}\">")
    meta_lines.append(f"<meta property='og:description' content=\"{og_desc}\">")
    if og_image:
        meta_lines.append(f"<meta property='og:image' content='{og_image}'>")
        meta_lines.append("<meta property='og:image:width' content='512'>")
        meta_lines.append("<meta property='og:image:height' content='512'>")
        meta_lines.append("<meta property='og:image:alt' content='ARAM Mayhem Database preview'>")
        meta_lines.append("<meta name='twitter:card' content='summary'>")
        meta_lines.append(f"<meta name='twitter:image' content='{og_image}'>")
        meta_lines.append("<meta name='twitter:image:alt' content='ARAM Mayhem Database preview'>")
    else:
        meta_lines.append("<meta name='twitter:card' content='summary'>")
    meta_lines.append(f"<meta name='twitter:title' content=\"{og_title}\">")
    meta_lines.append(f"<meta name='twitter:description' content=\"{og_desc}\">")
    if site_url:
        website_ld = {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": header_title,
            "alternateName": seo_title,
            "url": site_url,
            "description": seo_desc,
            "inLanguage": "zh-Hant",
        }
        meta_lines.append(
            "<script type='application/ld+json'>"
            + json.dumps(website_ld, ensure_ascii=False)
            + "</script>"
        )

    parts: list[str] = []
    parts.append("<!doctype html><html lang='zh-Hant'><head>")
    parts.extend(meta_lines)
    parts.extend(
        render_analytics_tags(
            cloudflare_token=cloudflare_analytics_token,
            ga_measurement_id=ga_measurement_id,
        )
    )
    # Webfonts: Noto Sans TC for everything by default; Noto Serif TC only
    # for a couple of small captions (subtitle, panel meta, augment lift)
    # where the mincho gives a "footnote" feel without hurting legibility.
    # `display=swap` lets system fallback paint immediately; weights pruned
    # to what each face actually uses on the page.
    parts.append(
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link href='https://fonts.googleapis.com/css2"
        "?family=Noto+Sans+TC:wght@400;500;600;700"
        "&family=Noto+Serif+TC:wght@400;500"
        "&display=swap' rel='stylesheet'>"
    )
    parts.append(f"<style>{css}</style></head><body>")
    # Header: brand + tab nav + language toggle.  GitHub star lives in the
    # page footer (with tier cutoffs / freshness) so it reads as a quiet
    # open-source credit instead of a header CTA.
    # The repo name is the canonical project URL; if the user later forks /
    # renames, update REPO_URL below.
    REPO_URL = "https://github.com/Lanternko/ARAM-Mayhem-Database"
    short_patch = display_patch if display_patch else "all patches"
    date_str = f"更新於 {build_date}" if build_date else "日期未標"
    globe_icon = (
        "<svg viewBox='0 0 24 24' width='16' height='16' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<circle cx='12' cy='12' r='10'></circle>"
        "<path d='M2 12h20'></path>"
        "<path d='M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10Z'></path>"
        "</svg>"
    )
    gh_icon = (
        "<svg viewBox='0 0 16 16' width='12' height='12' fill='currentColor' "
        "aria-hidden='true'><path d='M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1"
        "-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1."
        "23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-."
        "2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0"
        "-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12"
        "-.51.56-.82 1.27-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-"
        ".51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.2"
        "7.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.0"
        "1 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8"
        "Z'></path></svg>"
    )
    # Fixed top header: brand (= home) + content tabs + theme icon + language.
    # 主頁 / 設定 are not tabs — brand returns home; theme toggles in-header.
    # #site-title / #site-subtitle ids stay so applyLanguage drives brand text.
    # On narrow screens (<=700px) the header wraps: brand + actions on top,
    # .nav-tabs as a full-bleed scrollable strip underneath.
    NAV_TABS = (
        ("augments", "增幅榜", "Augment"),
        ("changes", "版本變動", "Patch Changes"),
        ("column", "專欄", "Column"),
    )
    sun_icon = (
        "<svg class='icon-sun' viewBox='0 0 24 24' width='16' height='16' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<circle cx='12' cy='12' r='4'></circle>"
        "<path d='M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41"
        "M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41'></path>"
        "</svg>"
    )
    moon_icon = (
        "<svg class='icon-moon' viewBox='0 0 24 24' width='16' height='16' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<path d='M21 14.5A8.5 8.5 0 1 1 9.5 3a7 7 0 0 0 11.5 11.5Z'></path>"
        "</svg>"
    )
    # data-nosnippet: without it Google's snippet fallback scrapes the nav
    # tabs / role chips into the search result blurb.
    parts.append("<header class='site-header' data-nosnippet>")
    parts.append("<div class='site-header-inner'>")
    parts.append(
        "<button class='brand' data-nav-tab='home' type='button' aria-label='arammeta' "
        "title='主頁'>"
        "<img class='brand-logo' src='favicon.svg' alt=''>"
        "<span class='brand-text'>"
        f"<span class='brand-title' id='site-title'>{header_title}</span>"
        f"<span class='brand-patch' id='site-subtitle'>{short_patch}</span>"
        "</span>"
        "</button>"
    )
    parts.append("<nav class='nav-tabs' role='tablist' aria-label='主要分頁'>")
    for i, (nav_key, nav_zh, nav_en) in enumerate(NAV_TABS):
        # No tab is active on first paint (home is the default, via brand).
        # First tab keeps tabindex=0 so the tablist stays keyboard-reachable.
        parts.append(
            f"<button class='nav-tab' id='tab-{nav_key}' "
            f"data-nav-tab='{nav_key}' role='tab' aria-controls='view-{nav_key}' "
            f"aria-selected='false' "
            f"tabindex='{'0' if i == 0 else '-1'}' "
            f"data-i18n-zh='{nav_zh}' data-i18n-en='{html.escape(nav_en)}'>{nav_zh}</button>"
        )
    parts.append("<span class='nav-ind' aria-hidden='true'></span>")
    parts.append("</nav>")
    parts.append("<div class='header-actions'>")
    parts.append(
        "<button class='icon-btn theme-toggle' id='theme-toggle' data-theme-toggle "
        "type='button' title='切換淺色' aria-label='切換主題'>"
        f"{sun_icon}{moon_icon}"
        "</button>"
    )
    parts.append(
        "<button class='icon-btn lang-toggle' id='lang-toggle' data-lang-toggle "
        "type='button' title='Switch to English' aria-label='切換語言'>"
        f"{globe_icon}<span id='lang-toggle-label'>EN</span>"
        "</button>"
    )
    parts.append("</div>")  # /header-actions
    parts.append("</div>")  # /site-header-inner
    parts.append("</header>")
    parts.append("<main class='site-main'>")
    # ---- View: 主頁 (home) — champion tier list + recommend panel ----
    parts.append(
        "<section class='view view-home is-active' id='view-home' "
        "data-view='home' aria-label='主頁'>"
    )
    parts.append("<div class='app-shell'>")
    parts.append("<div class='main-col'>")
    # Filter bar: role chips + free-text search + live "N shown" counter.
    parts.append("<div class='filter-bar' data-nosnippet>")
    parts.append("<div class='role-chips'>")
    parts.append('<button class="chip active" data-role="" data-label-zh="★ All" data-label-en="★ All">★ All</button>')
    for role_en in ROLE_ORDER:
        labels = ROLE_LABELS.get(role_en, {})
        role_zh = labels.get("zh", role_en)
        role_label_en = labels.get("en", role_en)
        parts.append(
            f'<button class="chip" data-role="{html.escape(role_en)}" data-label-zh="{html.escape(role_zh)}" '
            f'data-label-en="{html.escape(role_label_en)}">{html.escape(role_zh)}</button>'
    )
    parts.append("</div>")  # /role-chips
    parts.append("<div class='filter-tools'>")
    parts.append(
        '<button class="tool-btn" id="recommend-mode" type="button" '
        'aria-pressed="false">選擇你的隊友：關</button>'
    )
    # Search input wrapped in a label with an inline magnifier SVG sitting
    # in the input's left padding (the wrapper is positioned, the input
    # has padding-left to clear the icon).
    search_icon = (
        "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<circle cx='11' cy='11' r='7'></circle>"
        "<line x1='21' y1='21' x2='16.5' y2='16.5'></line></svg>"
    )
    parts.append(
        "<label class='search-wrap'>"
        f"{search_icon}"
        '<input class="search" id="champ-search" type="search" '
        'placeholder="搜尋英雄（中 / 英）" autocomplete="off" '
        'aria-label="搜尋英雄">'
        "</label>"
    )
    parts.append(
        f'<span class="shown-count"><span id="shown-n">{len(records)}</span> / {len(records)} '
        "<span id='shown-unit'>隻</span></span>"
    )
    parts.append("</div>")  # /filter-tools
    parts.append("</div>")  # /filter-bar

    for tier in TIER_ORDER:
        entries = by_tier[tier]
        if not entries:
            continue
        entries.sort(key=lambda d: -d["bayes_wr"])
        color = TIER_COLOR[tier]
        bg = TIER_LABEL_BG[tier]
        parts.append(
            f"<div class='tier-block' data-tier='{tier}' "
            f"style='--tier-color:{color}; --tier-bg:{bg};'>"
        )
        # New layout: tier name on its own heading row (no side bar), grid
        # takes the full row below.  Same look on desktop + mobile.
        parts.append("<h2 class='tier-heading'>")
        parts.append(f"<span class='tier-pill'><span>{tier}</span></span>")
        parts.append(
            f"<span class='tier-count'>"
            f"<span class='tier-count-num' data-tier='{tier}'>{len(entries)}</span>"
            " <span class='tier-count-unit'>隻</span>"
            "</span>"
        )
        parts.append("</h2>")
        parts.append("<div class='tier-grid'>")
        for r in entries:
            wr_pct = f"{r['bayes_wr'] * 100:.1f}%"
            meta = champ_meta.get(r["champion_id"], {})
            tags = list(meta.get("tags") or [])
            tag_str = " ".join(tags)
            primary_role = tags[0] if tags else ""
            secondary_role = tags[1] if len(tags) > 1 else ""
            alias = meta.get("alias", "")
            search_blob = _champ_search_blob(int(r["champion_id"]), r["name"], meta, tags)
            title = (
                f"{r['name']} · WR {wr_pct} · games {r['games']:,} · "
                f"raw {r['raw_wr']*100:.1f}%"
            )
            aria_label = f"{r['name']} {alias}，tier {tier}，勝率 {wr_pct}"
            parts.append(
                f"<div class='champ' data-cid='{r['champion_id']}' "
                f"data-name-zh=\"{html.escape(r['name'])}\" "
                f"data-name-en=\"{html.escape(meta.get('name_en', alias or r['name']))}\" "
                f"data-tags='{tag_str}' data-primary-role='{html.escape(primary_role)}' "
                f"data-secondary-role='{html.escape(secondary_role)}' "
                f"data-search=\"{html.escape(search_blob, quote=True)}\" "
                f"data-tier='{tier}' data-wr='{wr_pct}' data-games='{r['games']}' "
                f"data-raw-wr='{r['raw_wr']*100:.1f}%' "
                f"role='button' tabindex='0' "
                f"aria-label=\"{aria_label}\" "
                f"title=\"{title}\">"
                f"<img loading='lazy' src='{r['image']}' alt=''>"
                f"<span class='alt-role-badge' data-alt-role='{html.escape(primary_role)}' "
                "title='' aria-label='' hidden></span>"
                # The English alias is rendered as screen-reader-only text so
                # Ctrl+F / Cmd+F can find e.g. "Aatrox" even though only the
                # zh-TW name is drawn.  (aria-label already announces it for
                # actual screen readers.)
                f"<span class='sr-only'>{alias}</span>"
                f"<span class='wr'>{wr_pct}</span>"
                f"<span class='name'>{r['name']}</span>"
                f"</div>"
            )
        # Detail host lives INSIDE .tier-grid so it can grid-span all columns
        # and be inserted right after the clicked champion's visual row.
        parts.append(f"<div class='detail-host' data-tier='{tier}'></div>")
        parts.append("</div>")  # /tier-grid
        parts.append("</div>")  # /tier-block

    # Empty state — toggled by JS when all tiers are filtered out.
    parts.append(
        "<div class='empty-state' id='empty-state'>"
        "<strong id='empty-title'>沒有符合條件的英雄</strong>"
        "<span id='empty-copy'>換個角色篩選，或試試英雄中／英文名。</span>"
        "</div>"
    )

    parts.append("<div class='footer'>")
    parts.append(
        "<div class='cutoffs'>"
        "Tier (Bayes WR): "
        "<b>OP</b>≥55% · "
        "<b>T1</b>≥52% · "
        "<b>T2</b>≥50% · "
        "<b>T3</b>≥48% · "
        "<b>T4</b>≥46% · "
        "<b>T5</b>&lt;46%"
        "</div>"
    )
    if build_date:
        parts.append(
            f"<div class='freshness' id='freshness-copy'>{date_str}（{total_games:,} 場） · {patch_label}</div>"
        )
    # Footer open-source control: pill affordance so it reads as clickable,
    # still sits with freshness meta (not a header CTA).
    star_glyph = (
        "<svg viewBox='0 0 16 16' width='12' height='12' fill='currentColor' "
        "aria-hidden='true'><path d='M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 "
        "4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.751.751 0 0 "
        "1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L"
        ".818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 "
        "8 .25Z'></path></svg>"
    )
    parts.append(
        f"<a class='gh-star' href='{REPO_URL}' target='_blank' rel='noopener' "
        "aria-label='Star on GitHub' title='覺得有用請幫忙按 Star'>"
        f"{gh_icon}"
        "<span class='gh-star-label' data-i18n-zh='開源於 GitHub' "
        "data-i18n-en='Open source on GitHub'>開源於 GitHub</span>"
        f"<span class='gh-star-cta' aria-hidden='true'>{star_glyph}"
        "<span data-i18n-zh='Star' data-i18n-en='Star'>Star</span></span>"
        "</a>"
    )
    parts.append(
        "<div class='disclaimer'>"
        "This site isn't endorsed by Riot Games and doesn't reflect the views "
        "or opinions of Riot Games or anyone officially involved in producing "
        "or managing League of Legends. League of Legends and Riot Games are "
        "trademarks or registered trademarks of Riot Games, Inc. "
        "League of Legends © Riot Games, Inc."
        "</div>"
    )
    parts.append("</div>")
    parts.append("</div>")  # /main-col
    parts.append(
        # Start hidden so it never flashes on first paint before renderSidePanel()
        # runs (recommendMode defaults off → panel is hidden anyway). On mobile the
        # panel is a fixed full-screen overlay, so the pre-JS flash was very visible.
        "<aside class='side-panel is-hidden' id='side-panel'>"
        "<div class='side-head'>"
        "<div>"
        "<h2 id='side-title'>推薦組合排行</h2>"
        "<div class='side-sub' id='side-sub'>"
        "依歷史搭配排序，並修正傷害比例與陣容缺口。<br>"
        "推薦度越高越適合；可信度是資料穩定度摘要。"
        "</div>"
        "</div>"
        "<button class='side-close' id='side-close' type='button' aria-label='關閉推薦組合'>×</button>"
        "</div>"
        "<div class='pick-slots' id='pick-slots'></div>"
        "<div class='pick-note' id='pick-note'></div>"
        "<div class='rec-list' id='rec-list'></div>"
        "</aside>"
        "<button class='rec-fab is-hidden' id='rec-fab' type='button'>看推薦組合</button>"
    )
    parts.append("</div>")  # /app-shell
    parts.append("</section>")  # /view-home

    # ---- View: 增幅榜 (augments) — global per-augment WR tier, rendered by JS ----
    parts.append(
        "<section class='view view-augments' id='view-augments' data-view='augments' role='tabpanel' aria-labelledby='tab-augments'>"
        "<div class='view-narrow'>"
        "<h2 class='section-head' data-i18n-zh='增幅榜' data-i18n-en='Augment'>增幅榜</h2>"
        "<p class='section-sub' data-i18n-zh='每個增幅的整體勝率，依「同稀有度內」勝率排名分級；場數越多越可靠。' "
        "data-i18n-en='Overall win-rate of every augment, tiered by win-rate rank within its own rarity. More games = more reliable.'>"
        "每個增幅的整體勝率，依「同稀有度內」勝率排名分級；場數越多越可靠。</p>"
        "<div class='aug-tier-filters' id='aug-tier-filters'></div>"
        "<div id='aug-tier-host'></div>"
        "</div>"
        "</section>"
    )

    # ---- View: 版本變動 (changes) — patch-over-patch WR diff, rendered by JS ----
    parts.append(
        "<section class='view view-changes' id='view-changes' data-view='changes' role='tabpanel' aria-labelledby='tab-changes'>"
        "<div class='view-narrow'>"
        "<h2 class='section-head' data-i18n-zh='版本變動' data-i18n-en='Patch Changes'>版本變動</h2>"
        "<p class='section-sub' data-i18n-zh='與上一版相比，這版哪些英雄、裝備、英雄×裝備的勝率變化最大；場數越多越可靠。' "
        "data-i18n-en='Biggest win-rate shifts in heroes, items and hero×item versus the previous patch. More games = more reliable.'>"
        "與上一版相比，這版哪些英雄、裝備、英雄×裝備的勝率變化最大；場數越多越可靠。</p>"
        "<section class='updates-panel' id='updates-panel' aria-labelledby='updates-title'>"
        "<div class='updates-head'><div>"
        "<span class='updates-kicker' id='updates-kicker'></span>"
        "<h2 class='updates-title' id='updates-title'></h2>"
        "</div></div>"
        "<div class='updates-list' id='updates-list'></div>"
        "</section>"
        "</div>"
        "</section>"
    )

    # ---- View: 專欄 (column) — article list + reader, rendered by JS ----
    parts.append(
        "<section class='view view-column' id='view-column' data-view='column' role='tabpanel' aria-labelledby='tab-column'>"
        "<div class='view-column-host' id='column-host'></div>"
        "</section>"
    )

    # Theme + language live in the header; about / source sit in the home footer.
    parts.append("</main>")

    js = _read_site_template("site.js")
    payload_expr = (
        f"await loadSitePayload({json.dumps(payload_url, ensure_ascii=False)})"
        if payload_url
        else payload_json
    )
    js = "(async () => {\n" + js.strip() + "\n})().catch(err => {\n" \
        "    console.error(err);\n" \
        "    document.body.insertAdjacentHTML('afterbegin', " \
        "`<div style=\"margin:16px;padding:12px 14px;border:1px solid #7f1d1d;" \
        "background:#2a1216;color:#ffd7dc;border-radius:8px\">" \
        "資料載入失敗，請稍後再試。</div>`);\n" \
        "});"
    js = js.replace("__PAYLOAD__", payload_expr)
    js = js.replace("__HEADER_TITLE_ZH__", json.dumps(header_title, ensure_ascii=False))
    js = js.replace("__HEADER_TITLE_EN__", json.dumps(header_title_en, ensure_ascii=False))
    js = js.replace("__SHORT_PATCH_ZH__", json.dumps(short_patch, ensure_ascii=False))
    js = js.replace("__DATE_STR_ZH__", json.dumps(date_str, ensure_ascii=False))
    js = js.replace("__BUILD_DATE__", json.dumps(build_date, ensure_ascii=False))
    js = js.replace("__PATCH_LABEL__", json.dumps(patch_label, ensure_ascii=False))
    js = js.replace("__TOTAL_GAMES__", json.dumps(f"{total_games:,}", ensure_ascii=False))
    js = js.replace(
        "__ROLE_LABELS__",
        json.dumps(
            {
                "zh": {role: (ROLE_LABELS.get(role, {}).get("zh", role)) for role in ROLE_ORDER},
                "en": {role: (ROLE_LABELS.get(role, {}).get("en", role)) for role in ROLE_ORDER},
            },
            ensure_ascii=False,
        ),
    )
    parts.append(f"<script>{js}</script>")
    parts.append("</body></html>")
    return "".join(parts)


def _run_shell_only(
    *, out_path: Path, db: Path, queue_id: int, patch_prefix: str | None,
    payload_out: Path | None, payload_url: str, site_url: str, og_image: str,
    build_date: str, cloudflare_analytics_token: str, ga_measurement_id: str,
    min_pair_games: int, min_synergy_games: int,
) -> None:
    """Regenerate index.html from the existing payload, skipping all data compute.

    The slow part of a normal build is the win-rate + augment + item/affinity
    computation (it scans hundreds of thousands of games several times) that
    produces tier-list.json.  While iterating on the frontend (site.css /
    site.js / copy / columns) that data hasn't changed, so we reload the last
    payload, reconstruct only the inputs the HTML shell + server-rendered champ
    grid actually need (champ win-rates + names / tags / portraits) and re-render
    the page in ~seconds.  The server-side search blob is name-only here, but the
    client's enrichSearchIndexes() rebuilds the full search index from the loaded
    payload on init, so search behaves identically.  Run a normal (non-shell)
    build to refresh the underlying data / produce the exact production artifact.
    """
    import time
    t0 = time.time()
    payload_path = payload_out if payload_out is not None else (out_path.parent / "api" / "tier-list.json")
    if not payload_path.exists():
        raise click.ClickException(
            f"--shell-only needs an existing payload at {payload_path}; run a full build first."
        )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    champs = payload.get("champs") or {}
    if not champs:
        raise click.ClickException(f"{payload_path} has no champs; run a full build first.")

    # Reconstruct just what the shell + server grid need straight from the payload
    # (no DB win-rate / affinity compute).  champ_meta carries name / tags /
    # portrait; records carry the win-rates the grid + tier split read.
    records: list[dict] = []
    champ_meta: dict[int, dict] = {}
    for cid_s, c in champs.items():
        cid = int(cid_s)
        g = int(c.get("g") or 0)
        raw = float(c.get("rawWr") or 0.0)
        records.append({
            "champion_id": cid, "games": g, "wins": round(raw * g),
            "raw_wr": raw, "bayes_wr": float(c.get("wr") or 0.0),
        })
        champ_meta[cid] = {
            k: c.get(k)
            for k in ("name", "name_zh", "name_en", "alias", "tags", "image")
            if c.get(k) is not None
        }
    records.sort(key=lambda d: -d["bayes_wr"])

    # total_games headline: cheap index-backed COUNT; fall back to the payload sum.
    try:
        con = sqlite3.connect(str(db))
        if patch_prefix:
            total_games = con.execute(
                "SELECT COUNT(*) FROM games WHERE queue_id=? AND patch LIKE ?",
                (queue_id, f"{patch_prefix}%"),
            ).fetchone()[0]
        else:
            total_games = con.execute(
                "SELECT COUNT(*) FROM games WHERE queue_id=?", (queue_id,)
            ).fetchone()[0]
        con.close()
    except Exception:
        total_games = sum(r["games"] for r in records) // 10

    aug_meta = {
        int(k): v
        for k, v in (payload.get("augs") or {}).items()
        if str(k).lstrip("-").isdigit()
    }

    if not build_date:
        build_date = _dt.date.today().isoformat()
    if not og_image and site_url:
        og_image = site_url.rstrip("/") + "/og-image.png" + f"?v={build_date.replace('-', '')}-thumb"

    html = render_html(
        records, champ_meta,
        champ_profiles={}, champ_picks={}, champ_sets={}, champ_item_builds={},
        champ_single_items={}, champ_boot_items={}, champ_spell_items={},
        champ_item_clusters={}, champ_augment_types={}, champ_synergy={},
        aug_meta=aug_meta, patch_changes=payload.get("patchChanges") or {},
        new_aug_ids=frozenset(), queue_id=queue_id, patch_prefix=patch_prefix,
        ddragon_version="", total_games=total_games, min_games_per_pair=min_pair_games,
        min_synergy_games=min_synergy_games, site_url=site_url, og_image=og_image,
        build_date=build_date, cloudflare_analytics_token=cloudflare_analytics_token,
        ga_measurement_id=ga_measurement_id, payload_out_path=None,
        payload_url=payload_url or "api/tier-list.json", icon_assets_dir=None, aug_global=None,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    mirrors = write_spa_path_shells(out_path, site_url=site_url, og_image=og_image)
    click.echo(
        f"[shell-only] wrote {out_path} ({len(html):,} chars) in {time.time() - t0:.2f}s — "
        f"reused {payload_path.name}, skipped all win-rate / affinity compute"
    )
    if mirrors:
        click.echo(f"[shell-only] wrote {len(mirrors)} clean-path deep-link stubs (+ 404.html)")
