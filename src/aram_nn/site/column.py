"""Static, crawlable column (articles) pages.

Articles are editorial content, not app state: each one is a real HTML page at
``/column/<id>/`` (plus ``/en`` and ``/zh-cn`` mirrors) whose full text is in
the first response.  Sources live in ``scripts/templates/column/``:

* ``articles.json`` — ordered metadata (id, date, kicker, title, summary).
* ``<id>.zh.html`` / ``<id>.en.html`` — trusted body fragments.

zh-Hans is derived from the zh-Hant body with the same char table the app uses
(``names-zh-cn.json`` ``t2s``); champion names in name slots are swapped for the
official zh-Hans names first.  This module only assembles page parts; the
renderer owns the surrounding chrome so column pages match the other
information pages.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path

from aram_nn.site.champion_shell import _name_tables, _read_json_object, _t2s

LOCALES: tuple[tuple[str, str, str], ...] = (
    # (content language, html lang, URL prefix)
    ("zh", "zh-Hant", ""),
    ("zh-CN", "zh-Hans", "/zh-cn"),
    ("en", "en", "/en"),
)

_COPY = {
    "zh": {
        "index_title": "專欄",
        "index_eyebrow": "Articles",
        "index_description": "用實戰對局資料寫成的 ARAM 大亂鬥分析文章：英雄定位、增幅取捨與統計方法。",
        "index_intro": (
            "這裡的每篇文章都從 arammeta 收集的 Mayhem 大亂鬥對局出發，先定義指標、"
            "再給排名與反例，最後說明資料範圍與限制。數字會標示樣本與版本；"
            "它們描述的是歷史對局中的關聯，不是單場勝負的保證。"
        ),
        "published": "發布日期",
        "sep": "：",
        "more": "其他文章",
        "all": "所有文章",
        "tools_head": "接著看數據",
        "tools": (
            ("/", "英雄榜"), ("/augments/", "增幅勝率"),
            ("/draft/", "選角分析"), ("/changes/", "版本變動"),
        ),
        "nav": (("/", "首頁", "home"), ("/column/", "專欄", "column"),
                ("/about/", "關於", "about"), ("/feedback/", "回饋與聯絡", "feedback")),
        "chart_note": "圖表由目前公開的資料快照在建置時繪製，每個圓點是一位英雄。",
        "chart_x": "滾雪球能力 →",
        "chart_y": "後期能力 →",
        "chart_q": ("後期強", "滾雪球強 · 後期強", "兩者皆弱", "滾雪球強"),
        "chart_tip": "{name} · 後期 {late} · 滾雪球 {snow} · 勝率 {wr}",
        "chart_label": "英雄定位散佈圖：橫軸為滾雪球能力，縱軸為後期能力",
        "disclaimer": "arammeta 並未獲 Riot Games 認可，也不代表 Riot Games 或任何正式參與管理 Riot Games 相關資產者的觀點。Riot Games 與其相關資產為 Riot Games, Inc. 的商標或註冊商標。",
    },
    "en": {
        "index_title": "Articles",
        "index_eyebrow": "Column",
        "index_description": "Data-driven ARAM Mayhem analysis written from real matches: champion identity, augment trade-offs and the statistics behind the tier list.",
        "index_intro": (
            "Every article here starts from the ARAM Mayhem matches arammeta collects. "
            "Each one defines its metric first, then shows the ranking and the counter-examples, "
            "and closes with the data range and its limits. Numbers carry their sample and patch; "
            "they describe associations in past games, not a guarantee about any single match."
        ),
        "published": "Published",
        "sep": ": ",
        "more": "More articles",
        "all": "All articles",
        "tools_head": "Check the live data",
        "tools": (
            ("/en/", "Champion tier list"), ("/en/augments/", "Augment win rates"),
            ("/en/draft/", "Draft analysis"), ("/en/changes/", "Patch changes"),
        ),
        "nav": (("/en/", "Home", "home"), ("/en/column/", "Articles", "column"),
                ("/about/", "About", "about"), ("/en/feedback/", "Feedback", "feedback")),
        "chart_note": "The chart is drawn at build time from the current public data snapshot; each dot is one champion.",
        "chart_x": "Snowball →",
        "chart_y": "Late game →",
        "chart_q": ("Late game", "Snowball + late", "Neither", "Snowball"),
        "chart_tip": "{name} · late {late} · snowball {snow} · WR {wr}",
        "chart_label": "Champion map scatter plot: snowball on the x axis, late-game strength on the y axis",
        "disclaimer": "arammeta isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.",
    },
}

# Article body styles, layered on the information-page stylesheet.
COLUMN_CSS = """
.art-body h2 { margin: 40px 0 14px; }
.art-body p { margin: 14px 0; }
.art-body code { font-size: 0.92em; padding: 1px 5px; border: 1px solid var(--border); border-radius: 5px; }
.art-rank { display: grid; grid-template-columns: 26px 40px minmax(0, 1fr) auto; align-items: center; gap: 12px;
  padding: 9px 0; border-bottom: 1px solid var(--border); }
.art-rank .rk { color: var(--muted); font-size: 13px; font-weight: 700; text-align: right; font-variant-numeric: tabular-nums; }
.art-face { width: 40px; height: 40px; border-radius: 8px; background: var(--border); }
.art-meta { min-width: 0; }
.art-meta .nm { font-weight: 700; line-height: 1.3; }
.art-meta .sb { color: var(--muted); font-size: 13px; line-height: 1.45; }
.art-rank .lf { font-weight: 750; font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right; }
.art-rank .lf small { display: block; color: var(--muted); font-size: 11px; font-weight: 600; }
.art-build { display: flex; flex-wrap: wrap; gap: 10px; margin: 16px 0; }
.art-item { display: flex; align-items: center; gap: 8px; padding: 6px 10px 6px 6px;
  border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }
.art-item img { width: 32px; height: 32px; border-radius: 6px; }
.art-item .it-nm { font-size: 13px; font-weight: 650; line-height: 1.25; }
.art-item .it-tag { color: var(--muted); font-size: 11px; }
.scatter-wrap { margin: 22px 0; }
.scatter-chart svg { display: block; width: 100%; height: auto; }
.scatter-chart text { font-family: inherit; }
.scatter-legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; color: var(--muted); font-size: 13px; }
.scatter-legend .dot { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 50%; }
.art-cards { display: grid; gap: 14px; margin: 36px 0 0; padding: 0; list-style: none; }
.art-cards li { max-width: none; margin: 0; }
.art-card { display: block; padding: 18px 20px; border: 1px solid var(--border); border-radius: 12px;
  background: var(--surface); color: var(--text); text-decoration: none; }
.art-card:hover { border-color: var(--accent); }
.art-card .kk { color: var(--accent); font-size: 12px; font-weight: 750; letter-spacing: 0.08em; text-transform: uppercase; }
.art-card h2 { margin: 6px 0 8px; font-size: 20px; }
.art-card p { margin: 0; color: var(--muted); font-size: 15px; line-height: 1.65; }
.art-card time { display: block; margin-top: 10px; color: var(--muted); font-size: 12px; }
.art-links { display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 0; list-style: none; }
.art-links li { margin: 0; }
@media (max-width: 640px) {
  .art-rank { grid-template-columns: 20px 34px minmax(0, 1fr) auto; gap: 9px; }
  .art-face { width: 34px; height: 34px; }
}
"""


@dataclass(frozen=True)
class ColumnPage:
    relative_dir: str          # e.g. "column/skill-scaling" or "en/column"
    canonical_path: str        # e.g. "/en/column/skill-scaling/"
    html_lang: str
    title: str
    eyebrow: str
    description: str
    body_html: str
    updated: str
    updated_label: str
    updated_separator: str
    nav_items: tuple[tuple[str, str, str], ...]
    footer_disclaimer: str
    head_extra_html: str


def default_content_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "templates" / "column"


def load_articles(content_dir: Path | None = None) -> list[dict]:
    """Ordered article metadata with zh / en bodies; incomplete entries are skipped."""
    content_dir = Path(content_dir) if content_dir else default_content_dir()
    try:
        meta = json.loads((content_dir / "articles.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    articles: list[dict] = []
    for row in meta if isinstance(meta, list) else []:
        if not isinstance(row, dict):
            continue
        article_id = str(row.get("id") or "")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article_id):
            continue
        bodies = {}
        for lang in ("zh", "en"):
            path = content_dir / f"{article_id}.{lang}.html"
            if path.is_file():
                bodies[lang] = path.read_text(encoding="utf-8").strip()
        if set(bodies) != {"zh", "en"} or not row.get("title_zh") or not row.get("title_en"):
            continue
        articles.append({**row, "id": article_id, "body_zh": bodies["zh"], "body_en": bodies["en"]})
    return articles


def _champion_tables(api_dir: Path | None) -> tuple[dict, dict]:
    """(tier-list champs by id, official zh-Hans champion name by id)."""
    if api_dir is None:
        return {}, {}
    tier = _read_json_object(Path(api_dir) / "tier-list.json").get("champs") or {}
    raw = _read_json_object(Path(api_dir) / "names-zh-cn.json").get("champs") or {}
    cn: dict[str, str] = {}
    for cid, value in raw.items():
        name = value.get("n") if isinstance(value, dict) else value
        if isinstance(name, str) and name:
            cn[str(cid)] = name
    return {str(k): v for k, v in tier.items() if isinstance(v, dict)}, cn


_NAME_SLOT_RE = re.compile(r"""(class="nm">|alt="|<b>)([^<"]+)(?=</div>|"|</b>)""")


def _to_zh_hans(text: str, *, t2s_table: dict, champion_names: dict[str, str]) -> str:
    """zh-Hant → zh-Hans; champion names only in exact name slots.

    A bare replace would corrupt ordinary words (the champion 關 inside 關鍵),
    so names are swapped only where the whole slot is one champion name.
    """
    if champion_names:
        text = _NAME_SLOT_RE.sub(
            lambda m: m.group(1) + champion_names.get(m.group(2), m.group(2)), text,
        )
    return _t2s(text, t2s_table)


def _wr_color(wr: float) -> str:
    t = max(0.0, min(1.0, (wr - 0.44) / 0.12))
    stops = ((0.0, (214, 69, 69)), (0.5, (208, 178, 58)), (1.0, (72, 184, 104)))
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0)
            return "rgb(" + ",".join(str(round(a + (b - a) * f)) for a, b in zip(c0, c1)) + ")"
    return "rgb(72,184,104)"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def scatter_svg(api_dir: Path | None, *, copy: dict, name_of) -> str:
    """Static late-game × snowball scatter from the published axes snapshot."""
    if api_dir is None:
        return ""
    axes = _read_json_object(Path(api_dir) / "champ-empirical-axes.json").get("champs") or {}
    champs, _ = _champion_tables(api_dir)
    rows = []
    for cid, axis in axes.items():
        info = champs.get(str(cid))
        if not info or not isinstance(axis, dict):
            continue
        try:
            sx, sy, wr = float(axis["snowball"]), float(axis["scaling"]), float(info.get("wr") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((name_of(str(cid), info), sx, sy, wr))
    if len(rows) < 10:
        return ""
    xs, ys = [r[1] for r in rows], [r[2] for r in rows]
    width, height = 720, 520
    left, right, top, bottom = 50, 14, 14, 40
    x_pad = (max(xs) - min(xs)) * 0.06 or 0.1
    y_pad = (max(ys) - min(ys)) * 0.06 or 0.01
    x0, x1, y0, y1 = min(xs) - x_pad, max(xs) + x_pad, min(ys) - y_pad, max(ys) + y_pad
    plot_w, plot_h = width - left - right, height - top - bottom

    def px(v: float) -> float:
        return left + (v - x0) / (x1 - x0) * plot_w

    def py(v: float) -> float:
        return height - bottom - (v - y0) / (y1 - y0) * plot_h

    mx, my = px(_median(xs)), py(_median(ys))
    esc = html.escape
    q_tl, q_tr, q_bl, q_br = copy["chart_q"]
    out = [
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{esc(copy['chart_label'], quote=True)}'>",
        f"<rect x='{left}' y='{top}' width='{plot_w}' height='{plot_h}' rx='10' fill='var(--surface)' stroke='var(--border)'/>",
        f"<line x1='{mx:.1f}' y1='{top}' x2='{mx:.1f}' y2='{height - bottom}' stroke='var(--muted)' stroke-dasharray='5 5' opacity='.6'/>",
        f"<line x1='{left}' y1='{my:.1f}' x2='{width - right}' y2='{my:.1f}' stroke='var(--muted)' stroke-dasharray='5 5' opacity='.6'/>",
    ]
    quad = "fill='var(--muted)' font-size='13' font-weight='600'"
    out += [
        f"<text x='{left + 10}' y='{top + 20}' {quad}>{esc(q_tl)}</text>",
        f"<text x='{width - right - 10}' y='{top + 20}' text-anchor='end' {quad}>{esc(q_tr)}</text>",
        f"<text x='{left + 10}' y='{height - bottom - 10}' {quad}>{esc(q_bl)}</text>",
        f"<text x='{width - right - 10}' y='{height - bottom - 10}' text-anchor='end' {quad}>{esc(q_br)}</text>",
    ]
    tick = "fill='var(--muted)' font-size='11'"
    for v in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
        if x0 <= v <= x1:
            out.append(f"<text x='{px(v):.1f}' y='{height - bottom + 16}' text-anchor='middle' {tick}>{v:.1f}</text>")
    for v in (-0.10, -0.05, 0.0, 0.05, 0.10, 0.15):
        if y0 <= v <= y1:
            out.append(f"<text x='{left - 8}' y='{py(v) + 4:.1f}' text-anchor='end' {tick}>{v * 100:+.0f}%</text>")
    axis = "fill='var(--muted)' font-size='12' font-weight='600'"
    yc = top + plot_h / 2
    out.append(f"<text x='{left + plot_w / 2:.1f}' y='{height - 6}' text-anchor='middle' {axis}>{esc(copy['chart_x'])}</text>")
    out.append(f"<text x='14' y='{yc:.1f}' text-anchor='middle' transform='rotate(-90 14 {yc:.1f})' {axis}>{esc(copy['chart_y'])}</text>")

    for name, sx, sy, wr in rows:
        tip = copy["chart_tip"].format(
            name=name, late=f"{sy * 100:+.1f}pp", snow=f"{sx:.2f}", wr=f"{wr * 100:.1f}%",
        )
        out.append(
            f"<circle cx='{px(sx):.1f}' cy='{py(sy):.1f}' r='5.5' fill='{_wr_color(wr)}' "
            f"fill-opacity='.85' stroke='var(--bg)' stroke-width='1'><title>{esc(tip)}</title></circle>"
        )
    # Name the extremes of each axis so the picture reads without hovering.
    labelled: dict[str, tuple] = {}
    for key, reverse in ((1, True), (1, False), (2, True), (2, False)):
        for row in sorted(rows, key=lambda r: r[key], reverse=reverse)[:3]:
            labelled[row[0]] = row
    for name, sx, sy, _ in labelled.values():
        anchor, dx = ("end", -9) if px(sx) > width - right - 90 else ("start", 9)
        out.append(
            f"<text x='{px(sx) + dx:.1f}' y='{py(sy) + 4:.1f}' text-anchor='{anchor}' "
            f"fill='var(--text)' font-size='12' font-weight='650'>{esc(name)}</text>"
        )
    out.append("</svg>")
    return "".join(out)


def _article_path(prefix: str, article_id: str = "") -> str:
    return f"{prefix}/column/{article_id}/" if article_id else f"{prefix}/column/"


def _alternates(origin: str, article_id: str = "") -> str:
    if not origin.startswith("http"):
        return ""
    links = [
        ("zh-Hant", _article_path("", article_id)),
        ("zh-Hans", _article_path("/zh-cn", article_id)),
        ("en", _article_path("/en", article_id)),
        ("x-default", _article_path("", article_id)),
    ]
    return "".join(
        f"<link rel='alternate' hreflang='{lang}' href='{html.escape(origin + path, quote=True)}'>"
        for lang, path in links
    )


def _json_ld(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"<script type='application/ld+json'>{payload}</script>"


def column_pages(
    *, site_url: str = "", api_dir: Path | None = None, content_dir: Path | None = None,
) -> list[ColumnPage]:
    """Index + article pages for every locale, ready for the info-page chrome."""
    articles = load_articles(content_dir)
    if not articles:
        return []
    origin = (site_url or "").strip().rstrip("/")
    tables = _name_tables(api_dir)
    champs, cn_names = _champion_tables(api_dir)
    cn_by_zh = {
        str(info.get("name_zh") or info.get("name") or ""): cn_names[cid]
        for cid, info in champs.items() if cid in cn_names
    }
    cn_by_zh.pop("", None)

    pages: list[ColumnPage] = []
    for language, html_lang, prefix in LOCALES:
        source = "en" if language == "en" else "zh"
        copy = _COPY[source]

        def loc(text: str) -> str:
            if language != "zh-CN":
                return text
            return _to_zh_hans(text, t2s_table=tables["t2s"], champion_names=cn_by_zh)

        def nav_href(href: str) -> str:
            # zh-Hans reuses the zh-Hant copy; point its links at /zh-cn routes
            # that exist (home, column, feedback, product tabs).
            if language == "zh-CN" and href != "/about/":
                return "/zh-cn" + href
            return href

        def name_of(cid: str, info: dict) -> str:
            if language == "en":
                return str(info.get("name_en") or info.get("name") or "")
            zh = str(info.get("name_zh") or info.get("name") or "")
            if language == "zh-CN":
                return cn_names.get(cid) or _t2s(zh, tables["t2s"])
            return zh

        nav = tuple((nav_href(href), loc(label), key) for href, label, key in copy["nav"])
        tools = "".join(
            f"<li><a href='{nav_href(href)}'>{html.escape(loc(label))}</a></li>"
            for href, label in copy["tools"]
        )
        localized = []
        for article in articles:
            localized.append({
                "id": article["id"],
                "date": str(article.get("date") or ""),
                "kicker": loc(str(article.get(f"kicker_{source}") or "")),
                "title": loc(str(article[f"title_{source}"])),
                "summary": loc(str(article.get(f"summary_{source}") or "")),
                "body": loc(article[f"body_{source}"]),
            })

        cards = "".join(
            f"<li><a class='art-card' href='{_article_path(prefix, a['id'])}'>"
            f"<span class='kk'>{html.escape(a['kicker'])}</span>"
            f"<h2>{html.escape(a['title'])}</h2><p>{html.escape(a['summary'])}</p>"
            f"<time datetime='{html.escape(a['date'], quote=True)}'>{html.escape(a['date'])}</time></a></li>"
            for a in localized
        )
        index_path = _article_path(prefix)
        pages.append(ColumnPage(
            relative_dir=index_path.strip("/"),
            canonical_path=index_path,
            html_lang=html_lang,
            title=loc(copy["index_title"]),
            eyebrow=copy["index_eyebrow"],
            description=loc(copy["index_description"]),
            body_html=(
                f"<p>{html.escape(loc(copy['index_intro']))}</p>"
                f"<ul class='art-cards'>{cards}</ul>"
                f"<section><h2>{html.escape(loc(copy['tools_head']))}</h2>"
                f"<ul class='art-links'>{tools}</ul></section>"
            ),
            updated=max(a["date"] for a in localized),
            updated_label=loc(copy["published"]),
            updated_separator=copy["sep"],
            nav_items=nav,
            footer_disclaimer=loc(copy["disclaimer"]),
            head_extra_html=_alternates(origin, ""),
        ))

        chart = scatter_svg(api_dir, copy={k: loc(v) if isinstance(v, str) else tuple(loc(x) for x in v)
                                           for k, v in copy.items() if k.startswith("chart_")},
                            name_of=name_of)
        for a in localized:
            body = a["body"]
            if "id=\"scatter-host\"" in body:
                if chart:
                    body = body.replace(
                        "<div class=\"scatter-chart\" id=\"scatter-host\"></div>",
                        f"<div class='scatter-chart'>{chart}</div>"
                        f"<p class='updated'>{html.escape(loc(copy['chart_note']))}</p>",
                    )
                else:
                    body = re.sub(r"<div class=\"scatter-wrap\">.*?</div>\s*</div>", "", body, count=1, flags=re.S)
            others = "".join(
                f"<li><a href='{_article_path(prefix, o['id'])}'>{html.escape(o['title'])}</a></li>"
                for o in localized if o["id"] != a["id"]
            )
            path = _article_path(prefix, a["id"])
            head = _alternates(origin, a["id"])
            if origin.startswith("http"):
                head += _json_ld({
                    "@context": "https://schema.org",
                    "@type": "Article",
                    "headline": a["title"],
                    "description": a["summary"],
                    "datePublished": a["date"],
                    "inLanguage": html_lang,
                    "mainEntityOfPage": origin + path,
                    "author": {"@type": "Organization", "name": "arammeta", "url": origin + "/about/"},
                    "publisher": {"@type": "Organization", "name": "arammeta", "url": origin + "/"},
                })
            pages.append(ColumnPage(
                relative_dir=path.strip("/"),
                canonical_path=path,
                html_lang=html_lang,
                title=a["title"],
                eyebrow=a["kicker"],
                description=a["summary"],
                body_html=(
                    f"<article class='art-body'>{body}</article>"
                    f"<section><h2>{html.escape(loc(copy['more']))}</h2><ul>{others}</ul>"
                    f"<p><a href='{index_path}'>{html.escape(loc(copy['all']))}</a></p></section>"
                    f"<section><h2>{html.escape(loc(copy['tools_head']))}</h2>"
                    f"<ul class='art-links'>{tools}</ul></section>"
                ),
                updated=a["date"],
                updated_label=loc(copy["published"]),
                updated_separator=copy["sep"],
                nav_items=nav,
                footer_disclaimer=loc(copy["disclaimer"]),
                head_extra_html=head,
            ))
    return pages
