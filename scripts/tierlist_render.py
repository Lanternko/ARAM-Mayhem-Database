"""Tier-list site rendering (split from build_tier_list.py): payload assembly,
HTML shell, --shell-only fast preview, OG/favicon images, icon localization."""
from __future__ import annotations
import os as _os, sys as _sys
import time
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import tierlist_engine as _eng  # noqa: E402,F401
globals().update({_k: _v for _k, _v in vars(_eng).items() if not _k.startswith('__')})


ADSENSE_SITE_ORIGIN = "https://arammeta.com"
ADSENSE_CLIENT_ID = "ca-pub-8593280194977470"
ADSENSE_PUBLISHER_ID = "pub-8593280194977470"
ADSENSE_CERTIFICATION_AUTHORITY_ID = "f08c47fec0942fa0"


def render_adsense_verification_tag(*, site_url: str = "") -> str:
    """Return the production-only AdSense site verification script.

    The public publisher id is intentionally committed with the generated site.
    Keeping the tag production-only prevents previews and alternate hosts from
    creating ad requests under arammeta's account.
    """
    if (site_url or "").strip().rstrip("/") != ADSENSE_SITE_ORIGIN:
        return ""
    client = html.escape(ADSENSE_CLIENT_ID, quote=True)
    return (
        "<script async "
        "src='https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?"
        f"client={client}' crossorigin='anonymous'></script>"
    )



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
    """Write a compact favicon matching the flat Mayhem die mark (readable at 16–32px)."""
    # Flat die on rounded square — same language as mayhem-single-die-icon.png.
    # Avoid the old isometric cube + orbit (muddy at header/favicon sizes).
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'>
  <defs>
    <linearGradient id='die' x1='72' y1='56' x2='196' y2='208' gradientUnits='userSpaceOnUse'>
      <stop offset='0' stop-color='#f7fbff'/>
      <stop offset='0.45' stop-color='#d7e8ff'/>
      <stop offset='1' stop-color='#e8d6ff'/>
    </linearGradient>
  </defs>
  <rect x='8' y='8' width='240' height='240' rx='52' fill='#0b0f1a'/>
  <rect x='48' y='48' width='160' height='160' rx='36' fill='url(#die)'
        stroke='rgba(255,255,255,0.55)' stroke-width='6'/>
  <g fill='#0b0f1a'>
    <circle cx='96' cy='96' r='14'/>
    <circle cx='160' cy='96' r='14'/>
    <circle cx='96' cy='128' r='14'/>
    <circle cx='160' cy='128' r='14'/>
    <circle cx='96' cy='160' r='14'/>
    <circle cx='160' cy='160' r='14'/>
  </g>
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


_INFO_PAGE_CSS = """
:root {
  color-scheme: light dark;
  --bg: oklch(0.975 0.006 250);
  --surface: oklch(0.995 0.004 250);
  --text: oklch(0.245 0.018 250);
  --muted: oklch(0.49 0.018 250);
  --border: oklch(0.88 0.012 250);
  --accent: oklch(0.58 0.14 151);
  --accent-soft: oklch(0.94 0.035 151);
  --focus: oklch(0.68 0.15 151);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: oklch(0.17 0.012 250);
    --surface: oklch(0.205 0.014 250);
    --text: oklch(0.91 0.009 250);
    --muted: oklch(0.69 0.015 250);
    --border: oklch(0.31 0.016 250);
    --accent: oklch(0.71 0.14 151);
    --accent-soft: oklch(0.25 0.04 151);
    --focus: oklch(0.76 0.14 151);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 16px;
  line-height: 1.75;
  text-rendering: optimizeLegibility;
}
a { color: var(--accent); text-underline-offset: 0.18em; }
a:hover { text-decoration-thickness: 2px; }
a:focus-visible {
  outline: 3px solid color-mix(in oklch, var(--focus) 55%, transparent);
  outline-offset: 3px;
  border-radius: 4px;
}
.topbar {
  border-bottom: 1px solid var(--border);
  background: color-mix(in oklch, var(--surface) 92%, transparent);
}
.topbar-inner {
  width: min(100% - 32px, 980px);
  min-height: 64px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.brand {
  color: var(--text);
  font-size: 22px;
  font-weight: 720;
  letter-spacing: -0.045em;
  text-decoration: none;
}
.brand span { color: var(--accent); }
.topnav { display: flex; align-items: center; gap: 18px; }
.topnav a {
  color: var(--muted);
  font-size: 14px;
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
}
.topnav a:hover,
.topnav a[aria-current="page"] { color: var(--text); }
main {
  width: min(100% - 32px, 760px);
  margin: 0 auto;
  padding: 72px 0 88px;
}
.eyebrow {
  margin: 0 0 10px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 750;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
h1, h2 { line-height: 1.25; letter-spacing: -0.025em; }
h1 { margin: 0; font-size: 38px; }
h2 { margin: 0 0 14px; font-size: 22px; }
.lede {
  max-width: 62ch;
  margin: 20px 0 0;
  color: var(--muted);
  font-size: 18px;
  line-height: 1.7;
}
.updated { margin: 12px 0 0; color: var(--muted); font-size: 13px; }
section { margin-top: 44px; padding-top: 32px; border-top: 1px solid var(--border); }
p, li { max-width: 72ch; }
ul { padding-left: 1.25em; }
li + li { margin-top: 8px; }
.notice {
  margin-top: 28px;
  padding: 18px 20px;
  border: 1px solid color-mix(in oklch, var(--accent) 32%, var(--border));
  border-radius: 10px;
  background: var(--accent-soft);
}
.notice p { margin: 0; }
.action {
  display: inline-flex;
  min-height: 42px;
  margin-top: 12px;
  padding: 8px 16px;
  align-items: center;
  justify-content: center;
  border: 1px solid color-mix(in oklch, var(--accent) 55%, var(--border));
  border-radius: 8px;
  background: var(--accent);
  color: oklch(0.985 0.006 151);
  font-weight: 700;
  text-decoration: none;
}
.action:hover { filter: brightness(1.06); }
.page-footer {
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 12px;
}
.page-footer-inner {
  width: min(100% - 32px, 980px);
  margin: 0 auto;
  padding: 28px 0 34px;
}
.page-footer nav { display: flex; flex-wrap: wrap; gap: 10px 18px; }
.page-footer p { max-width: 90ch; margin: 18px 0 0; }
@media (max-width: 640px) {
  .topbar-inner { min-height: auto; padding: 14px 0; align-items: flex-start; flex-direction: column; gap: 10px; }
  .topnav { width: 100%; gap: 16px; overflow-x: auto; padding-bottom: 2px; }
  main { padding: 48px 0 64px; }
  h1 { font-size: 31px; }
  .lede { font-size: 17px; }
}
"""


def _info_page_html(
    *,
    slug: str,
    title: str,
    eyebrow: str,
    description: str,
    body_html: str,
    site_url: str,
    updated: str,
) -> str:
    """Render a lightweight, crawlable station-information page."""
    esc = html.escape
    base = _site_base_href(site_url) or "/"
    origin = base.rstrip("/")
    canonical_path = f"/{slug}/"
    canonical = (origin + canonical_path) if origin.startswith("http") else canonical_path
    nav_items = (
        ("/", "首頁", "home"),
        ("/about/", "關於", "about"),
        ("/privacy/", "隱私權", "privacy"),
        ("/contact/", "聯絡", "contact"),
    )
    nav = "".join(
        f"<a href='{href}'"
        + (" aria-current='page'" if key == slug else "")
        + f">{label}</a>"
        for href, label, key in nav_items
    )
    adsense = render_adsense_verification_tag(site_url=site_url)
    return (
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)} | arammeta</title>"
        f"<meta name='description' content='{esc(description, quote=True)}'>"
        f"<link rel='canonical' href='{esc(canonical, quote=True)}'>"
        "<link rel='icon' href='/favicon.svg' type='image/svg+xml'>"
        f"{adsense}<style>{_INFO_PAGE_CSS}</style></head><body>"
        "<header class='topbar'><div class='topbar-inner'>"
        "<a class='brand' href='/' aria-label='arammeta 首頁'>aram<span>meta</span></a>"
        f"<nav class='topnav' aria-label='站務導覽'>{nav}</nav>"
        "</div></header>"
        "<main>"
        f"<p class='eyebrow'>{esc(eyebrow)}</p><h1>{esc(title)}</h1>"
        f"<p class='lede'>{esc(description)}</p>"
        f"<p class='updated'>最後更新：{esc(updated)}</p>"
        f"{body_html}</main>"
        "<footer class='page-footer'><div class='page-footer-inner'>"
        f"<nav aria-label='頁尾導覽'>{nav}</nav>"
        "<p>arammeta 並未獲 Riot Games 認可，也不代表 Riot Games 或任何正式參與管理 Riot Games 相關資產者的觀點。"
        "Riot Games 與其相關資產為 Riot Games, Inc. 的商標或註冊商標。</p>"
        "</div></footer></body></html>\n"
    )


def write_site_info_pages(
    index_path: Path,
    *,
    site_url: str = "",
    build_date: str = "",
) -> list[Path]:
    """Write About, Privacy, Contact, and the production ads.txt file."""
    root = Path(index_path).parent
    updated = build_date or _dt.date.today().isoformat()
    repo_url = "https://github.com/Lanternko/ARAM-Mayhem-Database"
    issues_url = repo_url + "/issues/new"

    about_body = f"""
<section><h2>本站提供什麼</h2>
<p>arammeta 將 ARAM: Mayhem 對戰整理成英雄、增幅、裝備與隊伍搭配資料，並提供 Draft 分析與 Meta Pick 小遊戲。目標是讓玩家在短時間內看懂版本環境，同時保留樣本量與統計限制。</p></section>
<section><h2>資料怎麼來</h2>
<p>資料由本機 League Client 介面收集，網站只發布彙總後的對戰統計。公開資料不包含 PUUID、Riot ID、召喚師名稱或可識別個別玩家的原始紀錄。</p>
<ul><li>對局以 game ID 去除重複。</li><li>英雄勝率使用 Bayesian shrinkage，降低小樣本造成的極端波動。</li><li>版本、樣本數與更新日期會顯示在資料旁，跨版本結果不視為同一環境。</li></ul></section>
<section><h2>如何解讀</h2>
<p>勝率代表歷史資料中的關聯，不保證個別對局結果。英雄強度、玩家熟練度、隊伍組成、增幅選擇與版本平衡都會影響結果。樣本較少的組合應視為探索線索，不應當成確定答案。</p></section>
<section><h2>開源與回報</h2>
<p>網站與資料處理工具公開於 GitHub。你可以檢查方法、提出資料問題或回報介面錯誤。</p>
<p><a class="action" href="{repo_url}" target="_blank" rel="noopener">查看 GitHub 專案</a></p></section>
<div class="notice"><p>這是一個獨立社群專案，不是 Riot Games 官方網站，也未獲 Riot Games 贊助。</p></div>
"""
    privacy_body = f"""
<section><h2>我們處理哪些資料</h2>
<ul>
<li><strong>瀏覽與裝置資料：</strong>託管、API 與流量分析服務可能處理 IP 位址、瀏覽器類型、作業系統、來源頁、瀏覽路徑、國家或約略地區及效能資料，用於安全、除錯與流量統計。</li>
<li><strong>瀏覽器儲存：</strong>本站使用 localStorage 保存語言、主題、Meta Pick 暱稱草稿與頭像英雄；使用 sessionStorage 完成站內路徑導向。這些資料通常留在你的裝置上。</li>
<li><strong>Meta Pick 排行榜：</strong>選擇上傳成績時，暱稱、頭像英雄、五回合選擇、分數、版本與提交時間會傳送到後端。暱稱、頭像、分數及時間可能公開顯示，請勿使用真實姓名或其他個人資料。</li>
</ul></section>
<section><h2>使用目的</h2>
<p>資料用於提供網站功能、維護排行榜、偵測濫用、改善內容與效能，以及了解整體使用趨勢。我們不販售使用者個人資料。</p></section>
<section><h2>廣告與 Cookie</h2>
<p>本站申請使用 Google AdSense。第三方供應商（包括 Google）可能使用 Cookie，依使用者先前造訪本站或其他網站的情況投放廣告。Google 使用廣告 Cookie，可讓 Google 及其合作夥伴根據使用者造訪本站或網際網路上其他網站的情況顯示廣告。</p>
<p>你可以前往 <a href="https://adssettings.google.com/" target="_blank" rel="noopener">Google 廣告設定</a>停用個人化廣告，也可以透過 <a href="https://www.aboutads.info/choices/" target="_blank" rel="noopener">About Ads</a> 管理部分第三方供應商的個人化廣告選項。若所在地法令要求，本站會在載入個人化廣告前提供同意或拒絕選項。</p></section>
<section><h2>第三方服務</h2>
<p>本站可能使用 GitHub Pages 提供靜態網站、Cloudflare 提供流量分析與網路服務、Google 提供字型、分析或廣告服務，以及 arammeta 自有後端提供排行榜。這些服務會依各自的隱私政策處理必要資料。</p>
<ul><li><a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener">Cloudflare 隱私政策</a></li><li><a href="https://policies.google.com/privacy" target="_blank" rel="noopener">Google 隱私權政策</a></li><li><a href="https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement" target="_blank" rel="noopener">GitHub 隱私權聲明</a></li></ul></section>
<section><h2>保存與刪除</h2>
<p>託管與安全紀錄依服務供應商的保存政策處理。排行榜紀錄可能持續保存，直到例行維護、功能停止或收到合理的移除請求。彙總且無法識別個人的統計資料可能長期保留。</p></section>
<section><h2>查詢與請求</h2>
<p>若要詢問資料處理方式或要求移除排行榜紀錄，請透過聯絡頁提出。GitHub Issue 是公開頁面，請只描述需求，不要張貼 IP、帳號識別資訊或其他敏感資料。</p>
<p><a href="/contact/">前往聯絡與回報</a></p></section>
<div class="notice"><p>本政策可能隨功能、服務供應商或法令要求更新，重大變更會以更新日期標示。</p></div>
"""
    contact_body = f"""
<section><h2>適合回報的事項</h2>
<ul><li>英雄、增幅、裝備或版本資料異常。</li><li>手機版、無障礙、載入速度或互動錯誤。</li><li>Meta Pick 排行榜紀錄移除。</li><li>隱私權、廣告或站務問題。</li></ul>
<p><a class="action" href="{issues_url}" target="_blank" rel="noopener">建立 GitHub Issue</a></p></section>
<section><h2>隱私提醒</h2>
<p>GitHub Issue 會公開顯示。請勿貼上真實姓名、電子郵件、IP 位址、Riot ID、PUUID、驗證權杖或其他敏感資料。隱私請求只需提供排行榜暱稱、版本與大約提交時間，站方會視需要提供後續處理方式。</p></section>
<section><h2>處理方式</h2>
<p>請在標題簡述問題，並附上頁面網址、使用裝置與可重現步驟。資料問題若能附版本與畫面截圖，通常會更快定位。</p></section>
"""
    specs = (
        ("about", "關於 arammeta", "About", "ARAM Mayhem 的獨立資料工具、統計方法與開源資訊。", about_body),
        ("privacy", "隱私權政策", "Privacy", "arammeta 如何處理瀏覽資料、排行榜內容、Cookie 與第三方服務。", privacy_body),
        ("contact", "聯絡與回報", "Contact", "回報資料、介面、排行榜、隱私權與站務問題。", contact_body),
    )
    written: list[Path] = []
    for slug, title, eyebrow, description, body in specs:
        dest = root / slug / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            _info_page_html(
                slug=slug,
                title=title,
                eyebrow=eyebrow,
                description=description,
                body_html=body,
                site_url=site_url,
                updated=updated,
            ),
            encoding="utf-8",
        )
        written.append(dest)

    if (site_url or "").strip().rstrip("/") == ADSENSE_SITE_ORIGIN:
        ads_txt = root / "ads.txt"
        ads_txt.write_text(
            f"google.com, {ADSENSE_PUBLISHER_ID}, DIRECT, "
            f"{ADSENSE_CERTIFICATION_AUTHORITY_ID}\n",
            encoding="utf-8",
        )
        written.append(ads_txt)
    return written


def discover_column_article_ids(site_js: str | None = None) -> list[str]:
    """Article slugs from the ARTICLES array in site.js (shareable /column/<id>)."""
    text = site_js if site_js is not None else _read_site_template("site.js")
    m = re.search(r"const ARTICLES\s*=\s*\[(.*?)\n\s*\];", text, re.S)
    if not m:
        return []
    # Only top-level `id: 'slug'` entries inside the array body.
    return re.findall(r"(?m)^\s*id:\s*'([a-z0-9][a-z0-9-]*)'\s*,?\s*$", m.group(1))


# High-traffic History routes get a full copy of index.html (no bounce).
# Column *articles* stay as tiny stubs — low traffic, many paths.
SPA_FULL_SHELL_PATHS = frozenset({
    "/augments",
    "/draft",
    "/game",
    "/changes",
    "/column",
    "/en",
    "/en/augments",
    "/en/draft",
    "/en/game",
    "/en/changes",
    "/en/column",
    "/zh-CN",
    "/zh-CN/augments",
    "/zh-CN/draft",
    "/zh-CN/game",
    "/zh-CN/changes",
    "/zh-CN/column",
})

# Cap shipped per-champion detail rows.  UI carousels only show a handful;
# shipping the full ranked buckets was ~18 MB and dominated load time.
PAYLOAD_TOP_AUGS_PER_RARITY = 16
PAYLOAD_BOT_AUGS_PER_RARITY = 12
PAYLOAD_PAIRS_EACH_SIDE = 12
PAYLOAD_ITEM_PAIR_ROWS = 16
PAYLOAD_SINGLE_ITEM_ROWS = 24  # raised with the 1% pick floor (avg 17.4 rows/champ)

# Keep the initial tier-list payload focused on the grid, global augment index,
# Draft, and recommendation data.  These fields are only needed after a user
# opens one champion, so ship them as one small JSON shard per champion.
CHAMPION_DETAIL_FIELDS = (
    "bot",
    "sets",
    "items",
    "singleItems",
    "boots",
    "spells",
    "itemClusters",
    "augTypes",
)

# Draft Analysis final WR uses the same Composition LR as the local recommender
# (identity + team composition features).  Weights are exported as a compact JSON
# bundle at site-build time so the static page never imports sklearn/torch.
# Matchup formula (recommend_gui.predict_matchup_prob):
#   P(ally wins) = sigmoid(logit_ally − logit_enemy + intercept)
DRAFT_COMPOSITION_LR_DIR = Path("models/composition_lr_pooled_recency_7d")


def load_draft_composition_lr_payload(
    model_dir: Path = DRAFT_COMPOSITION_LR_DIR,
) -> dict | None:
    """Export Composition LR + champion profiles for browser 5v5 inference.

    Loaded only while building the site.  The bundle is ~60KB and contains no
    player data.  Prefer this over the old DeepSets export: the LR is what the
    recommender auto-refreshes and currently tracks the live patch.
    """
    model_dir = Path(model_dir)
    if not (model_dir / "model.pkl").exists():
        return None
    try:
        from aram_nn.recommend import (
            AD_BINS,
            CORE_COLUMNS,
            ENGAGE_GROUPS,
            FRONT_GROUPS,
            LACK_THRESHOLDS,
            POKE_GROUPS,
            ROLE_COLUMNS,
            SCORE_COLUMNS,
            WAVE_GROUPS,
            load_composition_lr,
        )

        model = load_composition_lr(model_dir)
        trained_through = None
        summary_path = model_dir / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                trained_through = summary.get("current_patch")
            except Exception:
                trained_through = None

        profiles: dict[str, dict] = {}
        for cid, profile in model.profiles.items():
            profiles[str(int(cid))] = {
                "scores": {
                    name: round(float(profile.scores[name]), 6) for name in SCORE_COLUMNS
                },
                "roles": {
                    role: round(float(profile.roles[role]), 6) for role in ROLE_COLUMNS
                },
                "physical_dpm": round(float(profile.physical_dpm), 4),
                "magic_dpm": round(float(profile.magic_dpm), 4),
                "true_dpm": round(float(profile.true_dpm), 4),
            }

        return {
            "kind": "composition_lr",
            "source_model": model_dir.name,
            "trained_through_patch": trained_through,
            "intercept": round(float(model.intercept), 8),
            "coef": [round(float(v), 8) for v in model.coef.tolist()],
            "feature_names": list(model.feature_names),
            "champ_to_idx": {str(int(k)): int(v) for k, v in model.champ_to_idx.items()},
            "profiles": profiles,
            "meta": {
                "score_columns": list(SCORE_COLUMNS),
                "core_columns": list(CORE_COLUMNS),
                "role_columns": list(ROLE_COLUMNS),
                "lack_thresholds": {k: float(v) for k, v in LACK_THRESHOLDS.items()},
                "ad_bins": list(AD_BINS),
                "front_groups": list(FRONT_GROUPS),
                "wave_groups": list(WAVE_GROUPS),
                "engage_groups": list(ENGAGE_GROUPS),
                "poke_groups": list(POKE_GROUPS),
            },
        }
    except Exception as exc:
        click.echo(f"[tierlist] WARN: unable to export Draft Composition LR: {exc}")
        return None


# Back-compat alias used by older call sites / notes.
load_draft_nn_payload = load_draft_composition_lr_payload


def slim_site_payload(payload: dict) -> dict:
    """In-place shrink of the public tier-list payload for faster first load.

    Caps ranked lists to what the UI actually renders (plus a small swipe buffer)
    and drops redundant fields.  Safe to re-run; returns a small stats dict.
    """
    champs = payload.get("champs") or {}
    before_rows = 0
    after_rows = 0
    for _cid, info in champs.items():
        if not isinstance(info, dict):
            continue
        for side, cap in (("top", PAYLOAD_TOP_AUGS_PER_RARITY), ("bot", PAYLOAD_BOT_AUGS_PER_RARITY)):
            buckets = info.get(side)
            if not isinstance(buckets, dict):
                continue
            for rar, rows in list(buckets.items()):
                if not isinstance(rows, list):
                    continue
                before_rows += len(rows)
                rows = rows[:cap]
                for row in rows:
                    if isinstance(row, dict):
                        row.pop("rawWr", None)
                buckets[rar] = rows
                after_rows += len(rows)
        pairs = info.get("pairs")
        if isinstance(pairs, list) and len(pairs) > PAYLOAD_PAIRS_EACH_SIDE * 2:
            before_rows += len(pairs)
            keep = PAYLOAD_PAIRS_EACH_SIDE
            info["pairs"] = pairs[:keep] + pairs[-keep:]
            after_rows += len(info["pairs"])
        elif isinstance(pairs, list):
            before_rows += len(pairs)
            after_rows += len(pairs)
        for key, cap in (
            ("items", PAYLOAD_ITEM_PAIR_ROWS),
            ("singleItems", PAYLOAD_SINGLE_ITEM_ROWS),
        ):
            bucket = info.get(key)
            if not isinstance(bucket, dict):
                continue
            for side, rows in list(bucket.items()):
                if not isinstance(rows, list):
                    continue
                before_rows += len(rows)
                rows = rows[:cap]
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if row.get("name") is not None and row.get("name") == row.get("name_zh"):
                        row.pop("name", None)
                    if row.get("peerGroup") == "global":
                        row.pop("peerGroup", None)
                    if row.get("peerScope") == "global":
                        row.pop("peerScope", None)
                bucket[side] = rows
                after_rows += len(rows)
        for key in ("boots", "sets", "itemClusters", "augTypes", "spells"):
            bucket = info.get(key)
            if not isinstance(bucket, dict):
                continue
            for side, rows in list(bucket.items()):
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if row.get("name") is not None and row.get("name") == row.get("name_zh"):
                        row.pop("name", None)
                    if row.get("peerGroup") == "global":
                        row.pop("peerGroup", None)
                    if row.get("peerScope") == "global":
                        row.pop("peerScope", None)
    return {"before_rows": before_rows, "after_rows": after_rows, "champs": len(champs)}


def split_champion_detail_payloads(payload: dict) -> dict[str, dict]:
    """Move detail-only champion fields out of the initial site payload.

    The returned mapping is ready to be written as ``champions/<cid>.json``.
    Re-running this on an already-split payload is a no-op.
    """
    details: dict[str, dict] = {}
    for cid, info in (payload.get("champs") or {}).items():
        if not isinstance(info, dict):
            continue
        detail = {
            key: info.pop(key)
            for key in CHAMPION_DETAIL_FIELDS
            if key in info
        }
        if detail:
            details[str(cid)] = detail
    return details


def champion_detail_base_url(payload_url: str) -> str:
    """Return the sibling ``champions`` URL for a tier-list payload URL."""
    clean = (payload_url or "api/tier-list.json").split("#", 1)[0].split("?", 1)[0]
    parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
    return f"{parent}/champions" if parent else "champions"


def write_champion_detail_shards(
    payload: dict,
    *,
    payload_out_path: Path,
    payload_url: str,
    version: str,
) -> dict[str, int]:
    """Split and write per-champion detail JSON next to the main payload."""
    details = split_champion_detail_payloads(payload)
    payload["detailBase"] = champion_detail_base_url(payload_url)
    payload["detailVersion"] = version
    if not details:
        return {"champs": 0, "bytes": 0}

    detail_dir = payload_out_path.parent / "champions"
    detail_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    for cid, detail in details.items():
        out = detail_dir / f"{cid}.json"
        out.write_text(
            json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        total_bytes += out.stat().st_size

    return {"champs": len(details), "bytes": total_bytes}


def versioned_payload_url(payload_url: str, version: str) -> str:
    """Append a cache-busting ?v= stamp so browsers can cache the big JSON."""
    url = (payload_url or "").strip()
    ver = (version or "").strip()
    if not url or not ver or "v=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}v={ver}"


def _localize_full_shell_html(
    html_src: str,
    *,
    site_url: str,
    canonical_path: str,
    html_lang: str,
    title: str,
    description: str,
) -> str:
    """Copy of the main SPA shell with locale/path-specific head tags."""
    base = _site_base_href(site_url) or "/"
    origin = base.rstrip("/")
    path = canonical_path if canonical_path.startswith("/") else f"/{canonical_path}"
    canonical = (origin + path) if origin.startswith("http") else path
    out = html_src
    out = re.sub(r"(<html\s+lang=)['\"][^'\"]*['\"]", rf"\1'{html_lang}'", out, count=1, flags=re.I)
    out = re.sub(
        r"(<link\s+rel=['\"]canonical['\"]\s+href=)['\"][^'\"]*['\"]",
        rf"\1'{html.escape(canonical, quote=True)}'",
        out,
        count=1,
        flags=re.I,
    )
    out = re.sub(
        r"(property=['\"]og:url['\"]\s+content=)['\"][^'\"]*['\"]",
        rf"\1'{html.escape(canonical, quote=True)}'",
        out,
        count=1,
        flags=re.I,
    )
    if title:
        out = re.sub(r"<title>[^<]*</title>", f"<title>{html.escape(title)}</title>", out, count=1, flags=re.I)
        out = re.sub(
            r"(property=['\"]og:title['\"]\s+content=)[\"'][^\"']*[\"']",
            rf'\1"{html.escape(title, quote=True)}"',
            out,
            count=1,
            flags=re.I,
        )
        out = re.sub(
            r"(name=['\"]twitter:title['\"]\s+content=)[\"'][^\"']*[\"']",
            rf'\1"{html.escape(title, quote=True)}"',
            out,
            count=1,
            flags=re.I,
        )
    if description:
        out = re.sub(
            r"(property=['\"]og:description['\"]\s+content=)[\"'][^\"']*[\"']",
            rf'\1"{html.escape(description, quote=True)}"',
            out,
            count=1,
            flags=re.I,
        )
        out = re.sub(
            r"(name=['\"]twitter:description['\"]\s+content=)[\"'][^\"']*[\"']",
            rf'\1"{html.escape(description, quote=True)}"',
            out,
            count=1,
            flags=re.I,
        )
    return out


def _spa_deep_link_stub(
    *,
    site_url: str = "",
    og_image: str = "",
    canonical_path: str = "/",
    title: str = "arammeta",
    description: str = "",
    html_lang: str = "zh-Hant",
) -> str:
    """Tiny GH Pages shell: stash path → bounce to / so the real SPA can boot.

    Used for long-tail article paths (many URLs, low traffic).  High-traffic
    routes get a full shell copy via write_spa_path_shells instead — bounce
    doubled LCP on /zh-CN and /en (~5s in analytics).
    """
    base = _site_base_href(site_url) or "/"
    origin = base.rstrip("/")
    path = canonical_path if canonical_path.startswith("/") else "/" + canonical_path
    canonical = (origin + path) if origin.startswith("http") else path
    desc = description or title
    og_img = og_image or ((origin + "/og-image.png") if origin.startswith("http") else "")
    esc = html.escape
    lang = html_lang if html_lang else "zh-Hant"
    if path == "/en" or path.startswith("/en/"):
        spa_lang = "en"
    elif path == "/zh-CN" or path.startswith("/zh-CN/"):
        spa_lang = "zh-CN"
    else:
        spa_lang = "zh"
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
        f"<!doctype html><html lang='{esc(lang, quote=True)}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title>"
        f"<link rel='canonical' href='{esc(canonical, quote=True)}'>"
        + "".join(og_bits)
        + "<script>"
        "try{"
        "sessionStorage.setItem('aram-spa-path',"
        "location.pathname+location.search+location.hash);"
        f"sessionStorage.setItem('aram-spa-lang','{spa_lang}');"
        "}catch(e){}"
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
    """Write deep-link shells + 404.html for clean path URLs on GH Pages.

    High-traffic routes (locale homes, main tabs) get a *full* copy of
    index.html so the browser never pays a bounce-to-/ double load.
    Long-tail column articles stay as tiny stubs that stash the path and
    redirect to / (sessionStorage restore in site.js).
    """
    index_path = Path(index_path)
    if not index_path.is_file():
        return []
    root = index_path.parent
    full_shell = index_path.read_text(encoding="utf-8")

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

    # (dest, canonical_path, title, description, html_lang)
    route_specs: list[tuple[Path, str, str, str, str]] = [
        (root / "404.html", "/", "arammeta", "", "zh-Hant"),
        (
            root / "augments" / "index.html",
            "/augments",
            "增幅 · arammeta",
            "ARAM 大亂鬥增幅勝率",
            "zh-Hant",
        ),
        (
            root / "draft" / "index.html",
            "/draft",
            "Draft · arammeta",
            "組隊 Draft：估計勝率與隊伍特性",
            "zh-Hant",
        ),
        (
            root / "game" / "index.html",
            "/game",
            "Meta Pick · arammeta",
            "挑選最佳陣容：小遊戲",
            "zh-Hant",
        ),
        (
            root / "changes" / "index.html",
            "/changes",
            "版本變動 · arammeta",
            "版本勝率變動",
            "zh-Hant",
        ),
        (
            root / "column" / "index.html",
            "/column",
            "專欄 · arammeta",
            "資料背後的思考與玩法解析",
            "zh-Hant",
        ),
        # English locale prefix mirrors (shareable /en… links).
        (root / "en" / "index.html", "/en", "arammeta", "ARAM Mayhem tier list", "en"),
        (
            root / "en" / "augments" / "index.html",
            "/en/augments",
            "Augments · arammeta",
            "ARAM Mayhem augment win rates",
            "en",
        ),
        (
            root / "en" / "draft" / "index.html",
            "/en/draft",
            "Draft · arammeta",
            "Team draft: estimated WR and composition traits",
            "en",
        ),
        (
            root / "en" / "game" / "index.html",
            "/en/game",
            "Meta Pick · arammeta",
            "Pick the best lineup: mini-game",
            "en",
        ),
        (
            root / "en" / "changes" / "index.html",
            "/en/changes",
            "Patch Changes · arammeta",
            "Patch-over-patch win-rate shifts",
            "en",
        ),
        (
            root / "en" / "column" / "index.html",
            "/en/column",
            "Articles · arammeta",
            "Data notes and play guides",
            "en",
        ),
        # Simplified Chinese locale prefix mirrors (shareable /zh-CN… links).
        (root / "zh-CN" / "index.html", "/zh-CN", "arammeta", "大乱斗强度榜", "zh-Hans"),
        (
            root / "zh-CN" / "augments" / "index.html",
            "/zh-CN/augments",
            "海克斯 · arammeta",
            "大乱斗海克斯胜率",
            "zh-Hans",
        ),
        (
            root / "zh-CN" / "draft" / "index.html",
            "/zh-CN/draft",
            "Draft · arammeta",
            "组队 Draft：估计胜率与队伍特性",
            "zh-Hans",
        ),
        (
            root / "zh-CN" / "game" / "index.html",
            "/zh-CN/game",
            "Meta Pick · arammeta",
            "挑选最佳阵容：小游戏",
            "zh-Hans",
        ),
        (
            root / "zh-CN" / "changes" / "index.html",
            "/zh-CN/changes",
            "版本变动 · arammeta",
            "版本胜率变动",
            "zh-Hans",
        ),
        (
            root / "zh-CN" / "column" / "index.html",
            "/zh-CN/column",
            "专栏 · arammeta",
            "数据背后的思考与玩法解析",
            "zh-Hans",
        ),
    ]
    for article_id, title in article_titles.items():
        route_specs.append(
            (
                root / "column" / article_id / "index.html",
                f"/column/{article_id}",
                f"{title} · arammeta",
                title,
                "zh-Hant",
            )
        )
        route_specs.append(
            (
                root / "en" / "column" / article_id / "index.html",
                f"/en/column/{article_id}",
                f"{title} · arammeta",
                title,
                "en",
            )
        )
        route_specs.append(
            (
                root / "zh-CN" / "column" / article_id / "index.html",
                f"/zh-CN/column/{article_id}",
                f"{title} · arammeta",
                title,
                "zh-Hans",
            )
        )

    written: list[Path] = []
    for dest, cpath, title, desc, html_lang in route_specs:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if cpath in SPA_FULL_SHELL_PATHS:
            body = _localize_full_shell_html(
                full_shell,
                site_url=site_url,
                canonical_path=cpath,
                html_lang=html_lang,
                title=title,
                description=desc,
            )
        else:
            body = _spa_deep_link_stub(
                site_url=site_url,
                og_image=og_image,
                canonical_path=cpath,
                title=title,
                description=desc,
                html_lang=html_lang,
            )
        dest.write_text(body, encoding="utf-8")
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
            # Style slug + filter role for 單件裝備強度 chips (shell can also
            # backfill via enrich_item_lut_styles on older payloads).
            _attach_item_style_fields(entry, meta)
        payload["itemLut"] = lut
        payload["ddv"] = version


def _item_filter_role_for_style(slug: str) -> str | None:
    """Map item_style slug → site role chip key for single-item filters.

    Assassin is explicit here (ad_assassin is not in ROLE_FROM_ITEM_STYLE).
    Marksman styles always map to Marksman for item filtering (unlike secondary
    role inference, which may reclassify melee champions as Fighter).
    """
    if not slug:
        return None
    if slug == "ad_assassin":
        return "Assassin"
    if slug in MARKSMAN_ITEM_STYLES:
        return "Marksman"
    return ROLE_FROM_ITEM_STYLE.get(slug)


# ARAM Guardian starters sit under ITEM_MIN_TOTAL_GOLD so item_style_infos
# intentionally skips them — but they still appear in 單件裝備強度 and need
# role chips.  IDs match GUARDIAN_STARTER_ITEM_IDS in tierlist_engine.
_GUARDIAN_ITEM_FILTER_ROLES: dict[int, tuple[str, ...]] = {
    2051: ("Tank",),       # Guardian's Horn / 保衛者號角
    3112: ("Mage",),       # Guardian's Orb / 保衛者冰玉
    3177: ("Fighter",),    # Guardian's Blade / 保衛者之刃
    3184: ("Marksman",),   # Guardian's Hammer / 保衛者戰鎚 (AD + lifesteal)
}


def _ordered_filter_roles(roles: list[str] | tuple[str, ...]) -> list[str]:
    """Stable unique roles in site ROLE_ORDER."""
    seen: set[str] = set()
    out: list[str] = []
    order = list(ROLE_ORDER) if "ROLE_ORDER" in globals() else [
        "Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank",
    ]
    rank = {r: i for i, r in enumerate(order)}
    for role in sorted({str(r) for r in roles if r}, key=lambda r: (rank.get(r, 99), r)):
        if role not in seen:
            seen.add(role)
            out.append(role)
    return out


def item_filter_roles_for_item(item: dict | None) -> list[str]:
    """Roles an item should match in 單件裝備強度 filter chips (may be multi).

    Separate from item_style_infos (affinity still uses one primary style):
    - Guardian starters get explicit roles despite the gold floor
    - Crit + AP hybrids (e.g. 殞落之祭 Rite of Ruin) match both Marksman & Mage
    """
    if not item:
        return []
    try:
        iid = int(item.get("id") or 0)
    except (TypeError, ValueError):
        iid = 0

    fixed = _GUARDIAN_ITEM_FILTER_ROLES.get(iid)
    if fixed:
        return list(fixed)

    categories = set(str(c) for c in (item.get("categories") or []))
    name = f"{item.get('name_en', '')} {item.get('name', '')}".lower()
    is_spell = "SpellDamage" in categories or "ability power" in name
    is_support = (
        "HealAndShieldPower" in categories
        or any(word in name for word in SUPPORT_ITEM_KEYWORDS)
    )

    # Name-fallback for alternate Guardian catalogue ids (e.g. 22xxxx mirrors).
    if "guardian's" in name or "保衛者" in str(item.get("name") or ""):
        if "SpellDamage" in categories:
            return ["Mage"]
        if "LifeSteal" in categories or "SpellVamp" in categories:
            return ["Marksman"]
        if {"ArmorPenetration", "Lethality"} & categories:
            return ["Assassin"]
        if "Damage" in categories:
            return ["Fighter"]
        if {"Health", "HealthRegen", "Armor", "SpellBlock"} & categories:
            return ["Tank"]

    # Crit + AP hybrid completed items: show under both 射手 and 法師.
    if (
        not is_support
        and "CriticalStrike" in categories
        and is_spell
        and int(item.get("price_total") or 0) >= ITEM_MIN_TOTAL_GOLD
    ):
        return _ordered_filter_roles(["Marksman", "Mage"])

    styles = item_style_infos(item)
    if not styles:
        return []
    role = _item_filter_role_for_style(str(styles[0].get("slug") or ""))
    return [role] if role else []


def _attach_item_style_fields(entry: dict, meta: dict | None) -> None:
    if not entry or not meta:
        return
    styles = item_style_infos(meta)
    if styles:
        slug = str(styles[0].get("slug") or "")
        if slug:
            entry["s"] = slug
    roles = item_filter_roles_for_item(meta)
    if roles:
        # Space-joined so the client can split the same way as data-item-role.
        entry["r"] = " ".join(roles)


def _item_role_overrides_path() -> Path:
    # Canonical human-labeled map (from exports/item-role-annotator.html).
    return Path(__file__).resolve().parent / "item_role_filter_overrides.json"


def load_item_role_filter_label_file(path: Path | None = None) -> dict:
    """Load hand-labeled item→role JSON (overrides + optional full_map)."""
    p = path or _item_role_overrides_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_filter_role_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split()
    elif isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value]
    else:
        return []
    return _ordered_filter_roles(parts)


def build_item_role_filter_map(
    *,
    ddragon_version: str | None = None,
    cache_dir: Path | None = None,
    overrides_path: Path | None = None,
) -> dict[str, list[str]]:
    """itemId → filter role list for 單件裝備強度 chips (injected into shell).

    1. Auto-classify from CommunityDragon / style heuristics (no games.db)
    2. Overlay ``full_map`` from scripts/item_role_filter_overrides.json if present
    3. Overlay ``overrides`` (hand diffs) last — empty list removes the item from
       all role chips

    Safe for shell-only deploys (does not touch tier-list.json).
    """
    try:
        item_meta = load_item_metadata(
            cache_dir=cache_dir or Path("data/cache"),
            ddragon_version=ddragon_version or None,
        )
    except Exception:
        item_meta = {}
    out: dict[str, list[str]] = {}
    for iid, meta in item_meta.items():
        # Ensure id is present for guardian / name fallbacks.
        if isinstance(meta, dict) and meta.get("id") is None:
            meta = {**meta, "id": iid}
        roles = item_filter_roles_for_item(meta)
        if roles:
            out[str(int(iid))] = roles

    data = load_item_role_filter_label_file(overrides_path)
    if not data:
        return out

    full_map = data.get("full_map") if isinstance(data.get("full_map"), dict) else {}
    for key, value in full_map.items():
        roles = _normalize_filter_role_list(value)
        kid = str(key)
        if roles:
            out[kid] = roles
        else:
            out.pop(kid, None)

    overrides = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    for key, value in overrides.items():
        roles = _normalize_filter_role_list(value)
        kid = str(key)
        if roles:
            out[kid] = roles
        else:
            out.pop(kid, None)
    return out


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
    script_assets_dir: Path | None = None,
    meta_pick_api_url: str = "",
    team_score_bundle: dict[str, object] | None = None,
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
                # wr stays the RAW observed rate: it is a fact about what happened
                # over `g` games and the tooltip presents it as such.  lift is the
                # shrunk estimate of how much of that is attributable to the
                # pairing, which the tooltip already labels "residual".  Publishing
                # expected+lift as "wr" instead would dress a counterfactual up as
                # an observation (a 44-game pair would read 36.7% after winning
                # 54.5%), so the two are deliberately allowed to differ.
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
            # Share of the headline wr contributed by the PREVIOUS patch's rate for
            # this champion (see CHAMP_PREV_PATCH_PRIOR_GAMES).  Near 1 on a day-old
            # patch, ~0 on a mature one.  Shipped so the card can say so out loud
            # instead of presenting a mostly-last-patch number as this patch's.
            "prevMix": round(
                float(champ_stat_by_cid.get(cid, {}).get("prev_mix", 0.0) or 0.0), 3
            ),
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

    trained_composition = dict((team_score_bundle or {}).get("composition") or {})
    recommendation_composition = {
        "weight": trained_composition.get("weight", RECOMMENDATION_COMPOSITION_WEIGHT),
        "clamp": trained_composition.get("clamp", RECOMMENDATION_COMPOSITION_CLAMP),
        "lack_thresholds": trained_composition.get(
            "lack_thresholds", COMPOSITION_LACK_THRESHOLDS
        ),
        "table_weights": trained_composition.get(
            "table_weights", RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS
        ),
        "tables": trained_composition.get("tables", RECOMMENDATION_COMPOSITION_TABLES),
        "damage_mix": {
            "target_ad_share": RECOMMENDATION_DAMAGE_MIX_TARGET_AD,
            "weight": RECOMMENDATION_DAMAGE_MIX_WEIGHT,
            "clamp": RECOMMENDATION_DAMAGE_MIX_CLAMP,
        },
    }
    for key in ("trained_patch", "trained_games", "cell_prior_games"):
        if key in trained_composition:
            recommendation_composition[key] = trained_composition[key]

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
        # Snapshot id for Meta Pick client/server parity (POST /api/meta-pick/runs).
        # Internal patch string (e.g. 16.11), not the public display map (26.11).
        "patch_prefix": patch_prefix or None,
        "min_games_per_pair": min_games_per_pair,
        "min_synergy_games": min_synergy_games,
        "patchChanges": patch_changes or {},
        "recommendation_composition": recommendation_composition,
        "team_score": dict((team_score_bundle or {}).get("team_score") or {}),
        "draftModel": load_draft_composition_lr_payload(),
    }
    if icon_assets_dir is not None:
        localize_cdragon_icons(payload, icon_assets_dir)
    _dedupe_item_objects(payload)
    slim_stats = slim_site_payload(payload)
    click.echo(
        f"[tierlist] slimmed payload rows {slim_stats['before_rows']:,} → "
        f"{slim_stats['after_rows']:,} across {slim_stats['champs']} champs"
    )
    # Cache-bust both the initial payload and its per-champion detail shards.
    # A timestamp suffix matters when multiple publishes happen on one day.
    payload_version = ""
    if payload_url and build_date:
        payload_version = f"{build_date.replace('-', '')}-{int(time.time())}"
        payload_url = versioned_payload_url(payload_url, payload_version)

    shard_stats = {"champs": 0, "bytes": 0}
    if payload_out_path is not None and payload_url:
        shard_stats = write_champion_detail_shards(
            payload,
            payload_out_path=payload_out_path,
            payload_url=payload_url,
            version=payload_version,
        )
        click.echo(
            f"[tierlist] wrote {shard_stats['champs']} champion detail shards "
            f"({shard_stats['bytes']:,} bytes total)"
        )
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
    # Browser tab / brand title is just "arammeta". SEO keywords live in
    # <meta description> + JSON-LD alternateName (not the tab chrome).
    patch_zh = f"版本 {display_patch} " if display_patch else ""
    page_title = header_title  # always "arammeta"
    seo_alternate = f"ARAM 大亂鬥（Mayhem）英雄勝率 Tier List・增幅與裝備數據｜{header_title}"
    seo_desc = (
        f"基於 {total_games:,} 場台服 ARAM 大亂鬥（Mayhem）實戰對局的英雄勝率排行、"
        f"增幅勝率、出裝與組隊推薦，{patch_zh}持續更新。"
    )

    meta_lines: list[str] = []
    meta_lines.append("<meta charset='utf-8'>")
    meta_lines.append(
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    )
    meta_lines.append(f"<title>{html.escape(page_title)}</title>")
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
            "alternateName": seo_alternate,
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
    adsense_tag = render_adsense_verification_tag(site_url=site_url)
    if adsense_tag:
        parts.append(adsense_tag)
    # Start the multi-MB tier-list JSON as early as possible — the SPA script
    # lives at end of <body>, so without preload the fetch only begins after
    # ~600KB of HTML/CSS/JS has been downloaded and parsed.
    if payload_url:
        preload_href = payload_url if (payload_url.startswith("http") or payload_url.startswith("/")) else payload_url
        parts.append(
            f"<link rel='preload' href='{html.escape(preload_href, quote=True)}' "
            "as='fetch' crossorigin='anonymous'>"
        )
    # Webfonts: Outfit = Latin brand wordmark only; Noto Sans TC = UI body;
    # Noto Serif TC = a few footnote captions (subtitle / panel meta / aug lift).
    # `display=swap` lets system fallback paint immediately.  The stylesheet is
    # loaded async (preload → flip to stylesheet onload): a render-blocking
    # cross-origin CSS fetch held first paint hostage to fonts.googleapis.com
    # while every glyph already has a swap fallback anyway.
    _fonts_css_url = (
        "https://fonts.googleapis.com/css2"
        "?family=Outfit:wght@500;600;700"
        "&family=Noto+Sans+TC:wght@400;500;600;700"
        "&family=Noto+Serif+TC:wght@400;500"
        "&display=swap"
    )
    parts.append(
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        f"<link rel='preload' as='style' href='{_fonts_css_url}' "
        "onload=\"this.onload=null;this.rel='stylesheet'\">"
        f"<noscript><link rel='stylesheet' href='{_fonts_css_url}'></noscript>"
    )
    # __SITE_JS_PRELOAD_SLOT__ is replaced at the end of this function: when the
    # site script is emitted as an external asset its content hash isn't known
    # yet while the <head> is being assembled.
    parts.append(f"__SITE_JS_PRELOAD_SLOT__<style>{css}</style></head><body>")
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
    # Fixed top header: brand (= home) + primary tabs + theme + language.
    # 「英雄」 is the home tier-list tab; brand also returns home.
    # Patch lives in the footer freshness line — not next to the wordmark.
    # On narrow screens (<=700px) the header wraps: brand + actions on top,
    # .nav-tabs as a full-bleed scrollable strip underneath.
    # (key, zh-TW, en, optional zh-CN override). Bare 增幅 is a product term
    # that does not t2s-convert — CN / aramkit call it 海克斯.
    NAV_TABS = (
        ("home", "英雄", "Champions", None),
        ("augments", "增幅", "Augments", "海克斯"),
        ("draft", "Draft", "Draft", None),
        ("game", "小遊戲", "Game", "小游戏"),
        ("changes", "版本變動", "Patch Changes", None),
        # 專欄 temporarily hidden from primary nav (routes/view still exist).
        # ("column", "專欄", "Articles", None),
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
        # Wordmark only in the header — no icon, no patch chip (favicon stays for the tab).
        # Weight split on aram/meta; both langs share the Latin mark.
        "<span class='brand-title' id='site-title' aria-label='arammeta'>"
        "<span class='brand-aram'>aram</span><span class='brand-meta'>meta</span>"
        "</span>"
        "</button>"
    )
    parts.append("<nav class='nav-tabs' role='tablist' aria-label='主要分頁'>")
    for i, (nav_key, nav_zh, nav_en, nav_zh_cn) in enumerate(NAV_TABS):
        # Home (= 英雄) is active on first paint; brand and this tab both land there.
        is_home = nav_key == "home"
        zh_cn_attr = (
            f" data-i18n-zh-cn='{html.escape(nav_zh_cn)}'" if nav_zh_cn else ""
        )
        parts.append(
            f"<button class='nav-tab{' active' if is_home else ''}' id='tab-{nav_key}' "
            f"data-nav-tab='{nav_key}' role='tab' aria-controls='view-{nav_key}' "
            f"aria-selected='{'true' if is_home else 'false'}' "
            f"tabindex='{'0' if is_home else '-1'}' "
            f"data-i18n-zh='{nav_zh}'{zh_cn_attr} data-i18n-en='{html.escape(nav_en)}'>{nav_zh}</button>"
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
    # Language menu (aramkit-style <details> dropdown): 繁體 / 简体 / English.
    parts.append(
        "<details class='lang-menu' id='lang-menu'>"
        "<summary class='icon-btn lang-toggle' id='lang-toggle' "
        "title='繁體中文' aria-label='語言: 繁體中文'>"
        f"{globe_icon}<span id='lang-toggle-label'>繁體中文</span>"
        "</summary>"
        "<div class='lang-menu-list' role='menu'>"
        "<button type='button' role='menuitem' data-lang='zh' class='is-active' "
        "aria-current='true'>繁體中文</button>"
        "<button type='button' role='menuitem' data-lang='zh-CN'>简体中文</button>"
        "<button type='button' role='menuitem' data-lang='en'>English</button>"
        "</div>"
        "</details>"
    )
    parts.append("</div>")  # /header-actions
    parts.append("</div>")  # /site-header-inner
    parts.append("</header>")
    parts.append("<main class='site-main'>")
    # ---- View: 主頁 (home) — champion tier list + recommend panel ----
    parts.append(
        "<section class='view view-home is-active' id='view-home' "
        "data-view='home' role='tabpanel' aria-labelledby='tab-home' "
        "aria-label='英雄'>"
    )
    parts.append("<div class='app-shell'>")
    parts.append("<div class='main-col'>")
    # Role chips scroll away; search-rail is a *sibling of the tier list*
    # (not nested in a short chrome row) so position:sticky survives detail
    # scroll.  CSS pulls the rail up into the same visual row as the chips.
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
    # Count sits immediately after the last role chip (Tank), not beside search.
    parts.append(
        f'<span class="shown-count"><span id="shown-n">{len(records)}</span> / {len(records)} '
        "<span id='shown-unit'>隻</span></span>"
    )
    parts.append("</div>")  # /role-chips
    parts.append("</div>")  # /filter-bar
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
    parts.append("<div class='search-rail' data-nosnippet>")
    parts.append(
        "<label class='search-wrap'>"
        f"{search_icon}"
        '<input class="search" id="champ-search" type="search" '
        'placeholder="搜尋英雄（中 / 英）" autocomplete="off" '
        'aria-label="搜尋英雄">'
        "</label>"
    )
    parts.append("</div>")  # /search-rail

    # Thin-patch disclosure.  Champion win rates are shrunk toward the previous
    # patch's rate (CHAMP_PREV_PATCH_PRIOR_GAMES); on a day-old patch that prior is
    # most of the displayed number.  prev_mix is driven by patch-wide sample size,
    # so it is near-identical across champions -- a property of the patch, not of
    # any one champion.  Hence one banner rather than a badge on all 170 tiles.
    # It disappears on its own once the patch matures past the 10% floor.
    _mixes = sorted(float(r.get("prev_mix") or 0.0) for r in records)
    _mix = _mixes[len(_mixes) // 2] if _mixes else 0.0
    _prev_patch = display_patch_prefix(previous_patch_prefix(patch_prefix))
    if _mix >= 0.10 and _prev_patch and display_patch:
        _mix_pct = f"{_mix * 100:.0f}%"
        _zh = (
            f"{display_patch} 目前 {total_games:,} 場，樣本還薄。勝率已混合上一版 "
            f"{_prev_patch} 的資料拉回（混合比重約 {_mix_pct}），避免改版首日的雜訊被當成強度。"
            "場數累積後混合會自動退場。"
        )
        _cn = (
            f"{display_patch} 目前 {total_games:,} 场，样本还薄。胜率已混合上一版 "
            f"{_prev_patch} 的数据拉回（混合比重约 {_mix_pct}），避免改版首日的噪声被当成强度。"
            "场数累积后混合会自动退场。"
        )
        _en = (
            f"{display_patch} has only {total_games:,} games so far. Win rates are "
            f"blended back toward {_prev_patch} (about {_mix_pct} of the number) so "
            "day-one noise is not read as strength. The blend fades out as games accumulate."
        )
        parts.append(
            "<div class='blend-note' data-nosnippet role='note' "
            f"data-i18n-zh=\"{html.escape(_zh, quote=True)}\" "
            f"data-i18n-zh-cn=\"{html.escape(_cn, quote=True)}\" "
            f"data-i18n-en=\"{html.escape(_en, quote=True)}\">"
            f"{html.escape(_zh)}</div>"
        )

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
            # Per-champion blend detail lives on the tooltip; the page-level
            # banner already carries the headline disclosure (see .blend-note).
            _pm = float(r.get("prev_mix") or 0.0)
            blend_hint = f" · 混合上版 {_pm*100:.0f}%" if _pm >= 0.10 else ""
            title = (
                f"{r['name']} · WR {wr_pct} · games {r['games']:,} · "
                f"raw {r['raw_wr']*100:.1f}%{blend_hint}"
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
    parts.append(
        "<nav class='site-links' aria-label='站務連結'>"
        "<a href='/about/' data-i18n-zh='關於' data-i18n-zh-cn='关于' "
        "data-i18n-en='About'>關於</a>"
        "<a href='/privacy/' data-i18n-zh='隱私權' data-i18n-zh-cn='隐私权' "
        "data-i18n-en='Privacy'>隱私權</a>"
        "<a href='/contact/' data-i18n-zh='聯絡' data-i18n-zh-cn='联系' "
        "data-i18n-en='Contact'>聯絡</a>"
        "</nav>"
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

    # ---- View: Draft — draftgap-style: ally rail | champion pool | enemy rail ----
    parts.append(
        "<section class='view view-draft' id='view-draft' data-view='draft' "
        "role='tabpanel' aria-labelledby='tab-draft'>"
        "<div class='draft-shell'>"
        "<aside class='draft-side is-ally' data-draft-side='ally'>"
        "<button type='button' class='draft-side-select is-active' data-draft-target='ally' "
        "id='draft-target-ally'>"
        "<span class='draft-side-label' data-i18n-zh='我方' data-i18n-en='Ally'>我方</span>"
        "<span class='draft-side-wr' id='draft-ally-wr'></span>"
        "</button>"
        "<div class='draft-slots' id='draft-ally-slots'></div>"
        "</aside>"
        "<div class='draft-main'>"
        "<div class='draft-mode-tabs' role='tablist' aria-label='Draft views'>"
        "<button type='button' class='draft-mode-tab is-active' id='draft-mode-draft' "
        "data-draft-view='draft' role='tab' aria-selected='true' "
        "data-i18n-zh='Draft' data-i18n-en='Draft'>Draft</button>"
        "<button type='button' class='draft-mode-tab' id='draft-mode-analysis' "
        "data-draft-view='analysis' role='tab' aria-selected='false' "
        "data-i18n-zh='Draft Analysis' data-i18n-en='Draft Analysis'>Draft Analysis</button>"
        "</div>"
        "<section class='draft-pane is-active' id='draft-pane-draft' data-draft-pane='draft' "
        "role='tabpanel' aria-labelledby='draft-mode-draft'>"
        "<div class='draft-picker-bar'>"
        "<label class='draft-search-wrap'>"
        f"{search_icon}"
        "<input class='search draft-search' id='draft-search' type='search' "
        "placeholder='搜尋英雄（中 / 英）' autocomplete='off' "
        "aria-label='搜尋英雄'>"
        "</label>"
        "<div class='draft-role-chips' id='draft-role-chips'></div>"
        "<button type='button' class='tool-btn ghost draft-clear' id='draft-clear' "
        "data-i18n-zh='清空' data-i18n-en='Clear'>清空</button>"
        "</div>"
        "<div class='draft-pool' id='draft-champ-list' role='listbox' "
        "aria-label='英雄列表' aria-multiselectable='true'></div>"
        "</section>"
        "<section class='draft-pane' id='draft-pane-analysis' data-draft-pane='analysis' "
        "role='tabpanel' aria-labelledby='draft-mode-analysis' hidden>"
        "<div class='draft-analysis-intro'>"
        "<div><span class='draft-analysis-kicker' data-i18n-zh='雙方陣容評估' data-i18n-en='ROSTER EVALUATION'>雙方陣容評估</span>"
        "<h2 data-i18n-zh='Draft Analysis' data-i18n-en='Draft Analysis'>Draft Analysis</h2></div>"
        "</div>"
        "<div class='draft-metrics' id='draft-metrics'></div>"
        "<div class='draft-result' id='draft-result'></div>"
        "</section>"
        "</div>"
        "<aside class='draft-side is-enemy' data-draft-side='enemy'>"
        "<button type='button' class='draft-side-select' data-draft-target='enemy' "
        "id='draft-target-enemy'>"
        "<span class='draft-side-label' data-i18n-zh='對手' data-i18n-en='Opponent'>對手</span>"
        "<span class='draft-side-wr' id='draft-enemy-wr'></span>"
        "</button>"
        "<div class='draft-slots' id='draft-enemy-slots'></div>"
        "</aside>"
        "</div>"
        "</section>"
    )

    # ---- View: Meta Pick mini-game — pick 5 of 10, hidden WR until reveal ----
    parts.append(
        "<section class='view view-game' id='view-game' data-view='game' "
        "role='tabpanel' aria-labelledby='tab-game'>"
        # Two mini-games share this view; the switcher is server-rendered so the
        # tab labels are in the HTML for crawlers, panels are toggled by JS.
        "<div class='game-mode-tabs' role='tablist' aria-label='小遊戲'>"
        "<button type='button' class='game-mode-tab is-active' data-game-mode='metapick' "
        "role='tab' aria-selected='true' "
        "data-i18n-zh='Meta Pick' data-i18n-en='Meta Pick'>Meta Pick</button>"
        "<button type='button' class='game-mode-tab' data-game-mode='augment' "
        "role='tab' aria-selected='false' "
        "data-i18n-zh='選增幅' data-i18n-zh-cn='选增幅' "
        "data-i18n-en='Augment Draft'>選增幅</button>"
        "</div>"
        "<div class='game-shell game-mode-panel' data-game-mode='metapick'>"
        "<header class='game-header'>"
        "<div class='game-header-top'>"
        "<div class='game-header-text'>"
        "<h2 data-i18n-zh='Meta Pick' data-i18n-en='Meta Pick'>Meta Pick</h2>"
        "<p class='game-sub' data-i18n-zh='5 回合挑戰：每回合從 10 隻英雄池挑最強 5 人隊（鎖定前不顯示勝率）' "
        "data-i18n-zh-cn='5 回合挑战：每回合从 10 只英雄池挑最强 5 人队（锁定前不显示胜率）' "
        "data-i18n-en='5-round challenge: each round pick the strongest 5-champ team from a 10-champ pool (WR hidden until lock)'>"
        "5 回合挑戰：每回合從 10 隻英雄池挑最強 5 人隊（鎖定前不顯示勝率）</p>"
        "</div>"
        # Hover/focus tip: how to think about team WR when playing Meta Pick.
        # data-i18n lives on leaf nodes only (applyLanguage sets textContent).
        "<button type='button' class='game-help-btn'>"
        "<span class='game-help-icon' aria-hidden='true'>?</span>"
        "<span class='sr-only' data-i18n-zh='遊玩建議' data-i18n-zh-cn='游玩建议' "
        "data-i18n-en='Play tips'>遊玩建議</span>"
        "<span class='game-help-tip' role='tooltip' "
        "data-i18n-zh='遊玩建議：勝率主要看英雄強度、搭配默契。"
        "再來看傷害組成比例（AP、AD 均衡），開戰和坦度是否足夠。"
        "先抓強勢英雄，再考慮合理搭配。' "
        "data-i18n-zh-cn='游玩建议：胜率主要看英雄强度、搭配默契。"
        "再来看伤害组成比例（AP、AD 均衡），开战和坦度是否足够。"
        "先抓强势英雄，再考虑合理搭配。' "
        "data-i18n-en='Tips: WR is driven mostly by champ strength and synergy. "
        "Then check damage mix (AP/AD balance), engage, and tankiness.\n"
        "Grab strong champions first, then build a reasonable team!'>"
        "遊玩建議：勝率主要看英雄強度、搭配默契。"
        "再來看傷害組成比例（AP、AD 均衡），開戰和坦度是否足夠。"
        "先抓強勢英雄，再考慮合理搭配。"
        "</span>"
        "</button>"
        "</div>"
        "</header>"
        "<div class='game-notice' id='game-notice' hidden></div>"
        "<div class='game-progress' id='game-progress'></div>"
        "<div class='game-slots' id='game-slots' aria-label='已選英雄'></div>"
        "<div class='game-pool' id='game-pool' role='listbox' aria-multiselectable='true' "
        "aria-label='英雄池'></div>"
        "<div class='game-result' id='game-result' hidden></div>"
        "<div class='game-settle' id='game-settle' hidden></div>"
        "<div class='game-footer' id='game-footer'>"
        "<div class='game-actions' id='game-actions'></div>"
        "</div>"
        "<section class='game-board' id='game-board' aria-labelledby='game-board-title'>"
        "<div class='game-board-head'>"
        "<h3 class='game-board-title' id='game-board-title' "
        "data-i18n-zh='全球排行榜' data-i18n-zh-cn='全球排行榜' "
        "data-i18n-en='Global leaderboard'>全球排行榜</h3>"
        "<p class='game-board-sub' id='game-board-sub' "
        "data-i18n-zh='5 回合平均排名（越低越好）' "
        "data-i18n-zh-cn='5 回合平均排名（越低越好）' "
        "data-i18n-en='Average rank over 5 rounds (lower is better)'>"
        "5 回合平均排名（越低越好）</p>"
        "</div>"
        "<div class='game-board-body' id='game-board-body'></div>"
        "</section>"
        "</div>"
        # 選增幅 — rendered entirely by JS (renderAugDraft), like #aug-tier-host.
        "<div class='game-shell game-mode-panel' data-game-mode='augment' "
        "id='aug-draft-host' hidden></div>"
        "</section>"
    )

    # ---- View: 增幅榜 (augments) — global per-augment WR tier, rendered by JS ----
    parts.append(
        "<section class='view view-augments' id='view-augments' data-view='augments' role='tabpanel' aria-labelledby='tab-augments'>"
        "<div class='view-narrow'>"
        "<h2 class='section-head' data-i18n-zh='增幅' data-i18n-zh-cn='海克斯' data-i18n-en='Augments'>增幅</h2>"
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
    # Empty string disables remote Meta Pick submit/leaderboard on the client.
    js = js.replace(
        "__META_PICK_API_BASE__",
        json.dumps((meta_pick_api_url or "").strip().rstrip("/"), ensure_ascii=False),
    )
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
    # Compact id→role map for 出裝 → 單件裝備強度 filter chips.  Regenerated on
    # every shell-only build from CDragon cache (no games.db); empty object if
    # the catalogue is unavailable so the client simply hides the chip bar.
    js = js.replace(
        "__ITEM_ROLES__",
        json.dumps(
            build_item_role_filter_map(ddragon_version=ddragon_version or None),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    # Split builds emit the app script as an external asset so the 15 full-SPA
    # shell copies (/, /en, /zh-CN, tab paths) share ONE browser-cached file
    # instead of each re-shipping ~100 KB gzip of inline JS on every visit.
    # The URL is resolved against window.location.origin at runtime — the same
    # rule loadSitePayload uses — so local previews load the LOCAL script even
    # though production HTML carries <base href='https://arammeta.com/'>, and
    # the /en/... shells resolve to the root asset instead of /en/assets/....
    # Stable filename + content-hash ?v= mirrors the tier-list.json convention:
    # a stale shell copy keeps working (query is ignored by the static host)
    # and the cache busts only when the script actually changes.
    site_js_preload = ""
    if script_assets_dir is not None and payload_url:
        import hashlib

        script_assets_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_assets_dir / "site.js"
        script_path.write_text(js, encoding="utf-8")
        js_ver = hashlib.sha1(js.encode("utf-8")).hexdigest()[:12]
        js_url_expr = f"window.location.origin+'/assets/site.js?v={js_ver}'"
        # Head: start the download during HTML parse (an inline injector because
        # a plain <link rel='preload'> href would resolve through <base> to the
        # production origin in local previews).
        site_js_preload = (
            "<script>(function(){var l=document.createElement('link');"
            "l.rel='preload';l.as='script';"
            f"l.href={js_url_expr};"
            "document.head.appendChild(l);})()</script>"
        )
        # End of body: DOM is fully parsed here, and the injected script only
        # executes after its download completes — same timing guarantees as the
        # old inline end-of-body script.
        parts.append(
            "<script>(function(){var s=document.createElement('script');"
            f"s.src={js_url_expr};"
            "document.head.appendChild(s);})()</script>"
        )
        click.echo(
            f"[tierlist] wrote {script_path}  ({script_path.stat().st_size:,} bytes, v={js_ver})"
        )
    else:
        parts.append(f"<script>{js}</script>")
    parts.append("</body></html>")
    return "".join(parts).replace("__SITE_JS_PRELOAD_SLOT__", site_js_preload, 1)


def _run_shell_only(
    *, out_path: Path, db: Path, queue_id: int, patch_prefix: str | None,
    payload_out: Path | None, payload_url: str, site_url: str, og_image: str,
    build_date: str, cloudflare_analytics_token: str, ga_measurement_id: str,
    min_pair_games: int, min_synergy_games: int,
    meta_pick_api_url: str = "",
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
    payload_text = payload_path.read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    # Stamp snapshot id when an older payload predates Meta Pick leaderboard.
    if patch_prefix and not payload.get("patch_prefix"):
        payload["patch_prefix"] = patch_prefix
    # Always (re)export Draft model on shell-only so migrations
    # (DeepSets → Composition LR) land without a multi-minute data rebuild.
    draft_model = load_draft_composition_lr_payload()
    if draft_model is not None:
        prev_kind = (payload.get("draftModel") or {}).get("kind")
        payload["draftModel"] = draft_model
        if prev_kind and prev_kind != draft_model.get("kind"):
            click.echo(
                f"[shell-only] draftModel {prev_kind} → {draft_model.get('kind')} "
                f"({draft_model.get('source_model')})"
            )
    elif not payload.get("draftModel"):
        click.echo("[shell-only] WARN: Draft Composition LR unavailable; final WR disabled")
    champs = payload.get("champs") or {}
    if not champs:
        raise click.ClickException(f"{payload_path} has no champs; run a full build first.")

    # Slim oversized payloads left over from older full builds (full ranked
    # aug/item lists).  Rewrite in place so the next fetch is smaller without a
    # multi-minute data rebuild.
    before_bytes = payload_path.stat().st_size
    slim_stats = slim_site_payload(payload)
    if not build_date:
        build_date = _dt.date.today().isoformat()
    payload_ver = build_date.replace("-", "")
    try:
        payload_ver = f"{payload_ver}-{int(payload_path.stat().st_mtime)}"
    except OSError:
        pass
    resolved_payload_url = versioned_payload_url(
        payload_url or "api/tier-list.json",
        payload_ver,
    )
    shard_stats = write_champion_detail_shards(
        payload,
        payload_out_path=payload_path,
        payload_url=resolved_payload_url,
        version=payload_ver,
    )
    slim_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if slim_json != payload_text or shard_stats["champs"]:
        payload_path.write_text(slim_json, encoding="utf-8")
        click.echo(
            f"[shell-only] slimmed {payload_path.name}: "
            f"{before_bytes / 1e6:.1f} MB → {len(slim_json.encode('utf-8')) / 1e6:.1f} MB "
            f"(rows {slim_stats['before_rows']:,} → {slim_stats['after_rows']:,})"
        )
    champs = payload.get("champs") or {}

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
            # Carried through so a frontend-only reship keeps the thin-patch
            # disclosure banner.  Without it a shell-only build during a fresh
            # patch would silently drop the .blend-note that the full build
            # rendered, leaving blended numbers on screen with nothing saying so.
            "prev_mix": float(c.get("prevMix") or 0.0),
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
        payload_url=resolved_payload_url, icon_assets_dir=None, aug_global=None,
        script_assets_dir=out_path.parent / "assets",
        meta_pick_api_url=meta_pick_api_url,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    mirrors = write_spa_path_shells(out_path, site_url=site_url, og_image=og_image)
    info_pages = write_site_info_pages(
        out_path,
        site_url=site_url,
        build_date=build_date,
    )
    full_n = sum(1 for p in mirrors if p.stat().st_size > 50_000)
    click.echo(
        f"[shell-only] wrote {out_path} ({len(html):,} chars) in {time.time() - t0:.2f}s — "
        f"reused {payload_path.name}, skipped all win-rate / affinity compute"
    )
    if mirrors:
        click.echo(
            f"[shell-only] wrote {len(mirrors)} clean-path shells "
            f"({full_n} full SPA, rest stubs + 404.html)"
        )
    if info_pages:
        click.echo(f"[shell-only] wrote {len(info_pages)} site information file(s)")
