"""Small, indexable champion pages sharing one cacheable interactive shell."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

# Extra static content beyond a champion's name + win rate.  The first HTML
# response for /champions/<slug>/ used to be ~150 visible characters while all
# augment / item rankings arrived via JS; Search Console treated hundreds of
# champion URLs as thin pages found but not indexed.  The rankings below are
# rendered at build time from the champion's own detail shard, so the page
# carries its real content before any script runs.
#
# Names are looked up in the payload files the shell build already ships:
# tier-list.json (`augs`, `itemLut`) carries zh-Hant / en, names-zh-cn.json
# carries the official zh-Hans names plus the renderer's char-level t2s table.
# A missing name falls back to another language's name, never to invented text.

_DESC_LIMIT = 160

# Rarity, section headings and list formatting per locale.  zh-Hant is the
# canonical product voice; zh-Hans and en keep the same information order.
_COPY: dict[str, dict] = {
    "zh-Hant": {
        "rarity": {"kPrismatic": "稜彩增幅", "kGold": "金色增幅", "kSilver": "銀色增幅"},
        "aug_head": "{rarity}前 {n} 名",
        "cluster_head": "核心出裝前 {n} 組",
        "boots_head": "鞋子前 {n} 名",
        "spells_head": "召喚師技能前 {n} 名",
        "games": "{n} 場",
        "list_sep": "、",
        "summary_augs": "{name}在 Mayhem 大亂鬥{patch}的稜彩增幅前 3 名為 {names}",
        "summary_patch": "（{patch}）",
        "summary_core": "核心出裝為 {items}",
        "summary_join": "；",
        "summary_end": "。",
    },
    "zh-Hans": {
        "rarity": {"kPrismatic": "棱彩海克斯", "kGold": "金色海克斯", "kSilver": "银色海克斯"},
        "aug_head": "{rarity}前 {n} 名",
        "cluster_head": "核心出装前 {n} 组",
        "boots_head": "鞋子前 {n} 名",
        "spells_head": "召唤师技能前 {n} 名",
        "games": "{n} 场",
        "list_sep": "、",
        "summary_augs": "{name}在 Mayhem 大乱斗{patch}的棱彩海克斯前 3 名为 {names}",
        "summary_patch": "（{patch}）",
        "summary_core": "核心出装为 {items}",
        "summary_join": "；",
        "summary_end": "。",
    },
    "en": {
        "rarity": {"kPrismatic": "prismatic", "kGold": "gold", "kSilver": "silver"},
        "aug_head": "Top {n} {rarity} augments",
        "cluster_head": "Top {n} core builds",
        "boots_head": "Top {n} boots",
        "spells_head": "Top {n} summoner spells",
        "games": "{n} games",
        "list_sep": ", ",
        "summary_augs": "Top prismatic augments for {name} in ARAM Mayhem{patch} are {names}",
        "summary_patch": " ({patch})",
        "summary_core": "core build: {items}",
        "summary_join": "; ",
        "summary_end": ".",
    },
}

_DESC_KEYS = {"description", "og:description", "twitter:description"}
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_KEY_RE = re.compile(r"""(?:name|property)\s*=\s*(['"])([^'"]+)\1""", re.I)
_META_CONTENT_RE = re.compile(r"""content\s*=\s*(['"])(.*?)\1""", re.I | re.S)
_BUILD_RE = re.compile(r"window\.__ARAM_BUILD__=(.*?);?</script>", flags=re.S)

_NAME_TABLES: dict[tuple, dict] = {}
_SHARDS: dict[tuple, dict] = {}
_EMPTY_TABLES: dict = {"augs": {}, "items": {}, "cn_augs": {}, "cn_items": {}, "t2s": {}}


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _bounded_cache(cache: dict, key: tuple, value: dict) -> dict:
    if len(cache) >= 8:
        cache.clear()
    cache[key] = value
    return value


def _name_tables(api_dir: Path | None) -> dict:
    """Augment / item names for all three locales, keyed by file signature."""
    if api_dir is None:
        return _EMPTY_TABLES
    api_dir = Path(api_dir)
    tier_path = api_dir / "tier-list.json"
    cn_path = api_dir / "names-zh-cn.json"
    key = (str(api_dir), _file_signature(tier_path), _file_signature(cn_path))
    cached = _NAME_TABLES.get(key)
    if cached is not None:
        return cached
    tier = _read_json_object(tier_path)
    cn = _read_json_object(cn_path)
    tables = {
        "augs": {str(k): v for k, v in (tier.get("augs") or {}).items() if isinstance(v, dict)},
        "items": {str(k): v for k, v in (tier.get("itemLut") or {}).items() if isinstance(v, dict)},
        "cn_augs": {str(k): v for k, v in (cn.get("augs") or {}).items() if isinstance(v, dict)},
        "cn_items": {str(k): v for k, v in (cn.get("items") or {}).items()},
        "t2s": {str(k): v for k, v in (cn.get("t2s") or {}).items() if isinstance(v, str)},
    }
    return _bounded_cache(_NAME_TABLES, key, tables)


def _champion_shard(api_dir: Path | None, cid: object) -> dict:
    if api_dir is None or cid in (None, ""):
        return {}
    path = Path(api_dir) / "champions" / f"{cid}.json"
    key = (str(path), _file_signature(path))
    cached = _SHARDS.get(key)
    if cached is not None:
        return cached
    return _bounded_cache(_SHARDS, key, _read_json_object(path))


def _cn_ui_terms(text: str) -> str:
    """Port of site.js ``cnUiTerms``: 增幅(裝置) → 海克斯, keeping rune names."""
    if not text:
        return text
    locks: list[str] = []

    def lock(match: re.Match) -> str:
        locks.append(match.group(0))
        return f"{len(locks) - 1}"

    out = re.sub(r"冰川增幅", lock, text)
    out = out.replace("增幅裝置", "海克斯").replace("增幅装置", "海克斯").replace("增幅", "海克斯")
    return re.sub(r"(\d+)", lambda m: locks[int(m.group(1))], out)


def _t2s(text: str, table: dict) -> str:
    """Port of site.js ``t2s``: char-level zh-Hant → zh-Hans, then CN terms."""
    if not text:
        return text
    out = "".join(table.get(ch, ch) for ch in str(text)) if table else str(text)
    return _cn_ui_terms(out)


def _augment_name(aid: object, language: str, tables: dict) -> str:
    key = str(aid)
    row = tables["augs"].get(key) or {}
    if language == "en":
        return str(row.get("name_en") or row.get("name_zh") or row.get("name") or "")
    if language == "zh-Hans":
        cn_row = tables["cn_augs"].get(key) or {}
        if cn_row.get("n"):
            return str(cn_row["n"])
        base = str(row.get("name_zh") or row.get("name") or "")
        if base:
            return _t2s(base, tables["t2s"])
        return str(row.get("name_en") or "")
    return str(row.get("name_zh") or row.get("name") or row.get("name_en") or "")


def _item_name(item_id: object, language: str, tables: dict) -> str:
    key = str(item_id)
    row = tables["items"].get(key) or {}
    if language == "en":
        return str(row.get("e") or row.get("z") or "")
    if language == "zh-Hans":
        direct = tables["cn_items"].get(key)
        if direct:
            return str(direct)
        base = str(row.get("z") or "")
        if base:
            return _t2s(base, tables["t2s"])
        return str(row.get("e") or "")
    return str(row.get("z") or row.get("e") or "")


def _row_name(row: dict, language: str, tables: dict) -> str:
    """Name for rows that already carry name_zh / name_en (boots, spells)."""
    if language == "en":
        return str(row.get("name_en") or row.get("name_zh") or "")
    if language == "zh-Hans":
        if row.get("name_cn"):
            return str(row["name_cn"])
        base = str(row.get("name_zh") or "")
        if base:
            return _t2s(base, tables["t2s"])
        return str(row.get("name_en") or "")
    return str(row.get("name_zh") or row.get("name_en") or "")


def _win_rate(value: object) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


def _games(value: object, language: str) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return ""
    return _COPY[language]["games"].format(n=f"{count:,}")


def _rows(entries, *, language: str, tables: dict, namer) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = namer(entry)
        wr = _win_rate(entry.get("wr"))
        games = _games(entry.get("g"), language)
        if not name or not wr or not games:
            continue
        rows.append((name, wr, games))
    return rows


def _list_html(rows: list[tuple[str, str, str]]) -> str:
    return "<ol>" + "".join(
        f"<li>{html.escape(name)} — {html.escape(wr)} · {html.escape(games)}</li>"
        for name, wr, games in rows
    ) + "</ol>"


def _cluster_rows(
    groups, *, language: str, tables: dict, limit: int,
) -> list[tuple[str, str, str, list[str]]]:
    rows: list[tuple[str, str, str, list[str]]] = []
    for group in (groups or [])[:limit]:
        if not isinstance(group, dict):
            continue
        names: list[str] = []
        for item in group.get("core") or []:
            if not isinstance(item, dict):
                continue
            name = _item_name(item.get("id"), language, tables)
            if name and name not in names:
                names.append(name)
        wr = _win_rate(group.get("wr"))
        games = _games(group.get("g"), language)
        if not names or not wr or not games:
            continue
        rows.append((" + ".join(names), wr, games, names))
    return rows


def _static_markup(
    shard: dict, *, language: str, champion_name: str, patch_label: str, tables: dict,
) -> tuple[str, list[str], list[str]]:
    """Build the crawlable rankings section plus the names used in metadata.

    Returns (markup, top prismatic augment names, first core build item names).
    Sections with no usable data are omitted entirely.
    """
    copy = _COPY[language]
    top = shard.get("top") if isinstance(shard.get("top"), dict) else {}
    blocks: list[str] = []
    prismatic: list[str] = []

    # Keep the shard's own order: it is the ranking the interactive UI shows.
    for rarity in ("kPrismatic", "kGold", "kSilver"):
        rows = _rows(
            (top.get(rarity) or [])[:5],
            language=language,
            tables=tables,
            namer=lambda entry: _augment_name(entry.get("id"), language, tables),
        )
        if not rows:
            continue
        if rarity == "kPrismatic":
            prismatic = [name for name, _, _ in rows[:3]]
        heading = copy["aug_head"].format(rarity=copy["rarity"][rarity], n=len(rows))
        blocks.append(f"<h2 class='section-head'>{html.escape(heading)}</h2>" + _list_html(rows))

    clusters = shard.get("itemClusters") if isinstance(shard.get("itemClusters"), dict) else {}
    cluster_rows = _cluster_rows(
        (clusters.get("groups") or [])[:3], language=language, tables=tables, limit=3,
    )
    if cluster_rows:
        heading = copy["cluster_head"].format(n=len(cluster_rows))
        blocks.append(f"<h2 class='section-head'>{html.escape(heading)}</h2>"
                      + _list_html([row[:3] for row in cluster_rows]))

    boots = shard.get("boots") if isinstance(shard.get("boots"), dict) else {}
    boots_rows = _rows(
        (boots.get("top") or [])[:2], language=language, tables=tables,
        namer=lambda entry: _row_name(entry, language, tables),
    )
    if boots_rows:
        heading = copy["boots_head"].format(n=len(boots_rows))
        blocks.append(f"<h2 class='section-head'>{html.escape(heading)}</h2>" + _list_html(boots_rows))

    spells = shard.get("spells") if isinstance(shard.get("spells"), dict) else {}
    spell_rows = _rows(
        (spells.get("top") or [])[:2], language=language, tables=tables,
        namer=lambda entry: _row_name(entry, language, tables),
    )
    if spell_rows:
        heading = copy["spells_head"].format(n=len(spell_rows))
        blocks.append(f"<h2 class='section-head'>{html.escape(heading)}</h2>" + _list_html(spell_rows))

    if not blocks:
        return "", [], []

    # Data-generated summary: only the rankings above, no claim about lift.
    patch = copy["summary_patch"].format(patch=patch_label) if patch_label else ""
    parts: list[str] = []
    if prismatic:
        parts.append(copy["summary_augs"].format(
            name=champion_name, patch=patch, names=copy["list_sep"].join(prismatic),
        ))
    core_names = list(cluster_rows[0][3]) if cluster_rows else []
    if core_names:
        parts.append(copy["summary_core"].format(items=copy["list_sep"].join(core_names)))
    summary = ""
    if parts:
        summary = f"<p>{html.escape(copy['summary_join'].join(parts) + copy['summary_end'])}</p>"

    markup = f"<section class='view-narrow' id='champion-static'>{summary}{''.join(blocks)}</section>"
    return markup, prismatic, core_names


def _append_description(html_src: str, names: list[str], language: str) -> str:
    """Append the top prismatic augment names to description/og/twitter metas."""
    if not names:
        return html_src
    sep = _COPY[language]["list_sep"]
    suffix = ""
    for keep in (3, 2, 1):
        candidate = sep.join(names[:keep])
        if len(candidate) <= _DESC_LIMIT:
            suffix = candidate
            break
    if not suffix:
        return html_src

    def repl(match: re.Match) -> str:
        tag = match.group(0)
        key = _META_KEY_RE.search(tag)
        if not key or key.group(2).strip().lower() not in _DESC_KEYS:
            return tag
        content = _META_CONTENT_RE.search(tag)
        if not content:
            return tag
        base = html.unescape(content.group(2)).strip()
        if not base:
            return tag
        combined = f"{base} · {suffix}"
        if len(combined) > _DESC_LIMIT:
            return tag
        return (
            tag[: content.start(2)]
            + html.escape(combined, quote=True)
            + tag[content.end(2):]
        )

    return _META_TAG_RE.sub(repl, html_src)


def champion_shell_html(
    localized_html: str, *, route: dict, record: dict, language: str,
    shell_url: str, loader_url: str, api_dir: Path | None = None,
) -> str:
    """Keep the route's head and meaningful snapshot text, without redirecting."""
    match = _BUILD_RE.search(localized_html)
    snapshot = json.loads(match[1]) if match else {}
    names = {"zh-Hant": "name_zh", "zh-Hans": "name_cn", "en": "name_en"}
    copy = {
        "zh-Hant": ("Bayesian 調整勝率", "英雄樣本數", "版本", "台服 · Mayhem · queue 2400", "正在載入增幅與出裝…", "查看英雄榜", "混合上版", "歷史對局關聯，非單場勝負保證。"),
        "zh-Hans": ("Bayesian 调整胜率", "英雄样本数", "版本", "台服 · Mayhem · queue 2400", "正在载入海克斯与出装…", "查看英雄榜", "混合上版", "历史对局关联，非单场胜负保证。"),
        "en": ("Bayesian win rate", "Champion sample", "Patch", "TW · Mayhem · queue 2400", "Loading augments and build…", "Champion tier list", "Previous-patch blend", "Historical association, not a guarantee of a match outcome."),
    }[language]
    name = html.escape(route[names[language]])
    prefix = {"zh-Hant": "", "zh-Hans": "/zh-cn", "en": "/en"}[language]
    patch_label = str(snapshot.get("patchLabel") or "").strip()
    tables = _name_tables(api_dir)
    shard = _champion_shard(api_dir, route.get("cid"))
    static, prismatic, _ = _static_markup(
        shard, language=language, champion_name=str(route[names[language]]),
        patch_label=patch_label, tables=tables,
    )
    stats = ""
    if record:
        stats = (
            f"<p>{copy[0]}: <strong>{float(record['bayes_wr']) * 100:.1f}%</strong>"
            f" · {copy[1]}: {int(record['games']):,}</p>"
        )
        previous = float(record.get("prev_mix") or 0)
        if previous >= .1:
            stats += f"<p class='section-sub'>{copy[6]}: {previous * 100:.0f}%</p>"
    fallback = (
        f"<main class='site-main' id='champion-fallback'><div class='view-narrow'>"
        f"<h1 class='section-head'>{name}</h1>{stats}"
        f"<p class='section-sub'>{copy[3]} · {copy[2]}: {html.escape(patch_label)}</p>"
        f"<p class='section-sub'>{html.escape(snapshot.get('buildDate', ''))} · {copy[7]}</p>"
        f"<p class='section-sub' id='champion-load-status' role='status'>{copy[4]}</p>"
        f"<a href='{prefix}/'>{copy[5]}</a></div></main>"
    )
    head = re.split(r"<body\b[^>]*>", localized_html, maxsplit=1, flags=re.I)[0]
    # Fetch the shell alongside the head's payload preloads. Discovered only by
    # the loader, it queued behind them and left the bare fallback up for ~20s
    # on a slow link; `crossorigin` is what lets the loader's fetch() reuse it.
    shell_preload = (
        f"<link rel='preload' as='fetch' crossorigin "
        f"href='{html.escape(shell_url, quote=True)}'>"
    )
    head, closed = re.subn(r"</head>", lambda m: shell_preload + m[0], head, count=1, flags=re.I)
    if not closed:
        head += shell_preload
    head = _append_description(head, prismatic, language)
    return (
        head + "<body>" + fallback + static
        # A deferred end-of-body script is low priority by default, which starved
        # this 2KB loader behind the 680KB payload.
        + f"<script src='{html.escape(loader_url, quote=True)}' fetchpriority='high' "
        + f"data-app-shell='{html.escape(shell_url, quote=True)}' defer></script>"
        + "</body></html>\n"
    )
