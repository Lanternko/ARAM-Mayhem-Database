"""Data-written analysis paragraphs for each champion page.

The champion page's crawlable HTML used to be ranked lists only (augments,
builds, spells) with almost no prose, which reads as auto-generated filler to
review crawlers.  These paragraphs interpret the same published data in plain
language: overall standing, which augments to take or skip, popular-but-weak
items, early/late tempo and team-composition fit.

Every sentence is derived from numbers the site already publishes, and the
copy states associations, never causes.  The same payload is rendered twice:
into the static HTML of /champions/<slug>/ and, via a small JSON file, into
the interactive page's "Other" tab, so people and crawlers see the same text.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from aram_nn.site.champion_shell import (
    _augment_name,
    _bounded_cache,
    _file_signature,
    _item_name,
    _name_tables,
    _read_json_object,
    _t2s,
)

LANGUAGES = ("zh-Hant", "zh-Hans", "en")

# Thresholds keep thin cells out of the prose.
_MIN_AUG_GAMES = 150
_MIN_BAD_ITEM_PICK = 0.10
_MIN_BAD_ITEM_LIFT_PP = 0.5
_MIN_FIT_PP = 0.3
_STAGE_PR_LO = 40  # same bands as site.js STAGE_PR_LO / STAGE_PR_HI
_STAGE_PR_HI = 60

_COPY: dict[str, dict] = {
    "zh-Hant": {
        "title": "{name} 大亂鬥分析",
        "sep": "、",
        "clause_sep": "；",
        "strength": "{name}在 Mayhem 大亂鬥{patch}的 Bayesian 調整勝率為 {wr}，在 {total} 位英雄中排第 {rank} 名，樣本 {games} 場。{verdict}",
        "patch": "（{patch} 版本）",
        "verdict_top": "屬於目前版本的強勢英雄。",
        "verdict_bottom": "目前版本偏弱，選到時更值得參考增幅與出裝的選擇。",
        "verdict_mid": "強度落在中段。",
        "aug_good": "稜彩增幅裡，{name}表現最好的是{names}",
        "aug_bad": "{names}在{name}身上勝率偏低，三選一時可以優先避開",
        "aug_item": "{aug}（{wr}，{games} 場）",
        "aug_join": "；相對地，",
        "end": "。",
        "bad_item": "常見但勝率偏低的裝備：{items}。這是歷史關聯，可能反映對局情境（例如落後時才補的裝），不一定是裝備本身不好。",
        "bad_item_one": "{item}有 {pick} 的{name}對局會出，勝率比{name}平均低 {lift} 個百分點",
        "stage_late": "比起 16 分鐘內結束的短局，{name}在 22 分鐘以上的長局勝率{dir} {diff} 個百分點，這個差距在 {total} 位英雄中排第 {rank}（越前面越偏後期），屬於後期型英雄。",
        "stage_early": "比起 22 分鐘以上的長局，{name}在 16 分鐘內結束的短局勝率表現較好（長局減短局為 {signed} 個百分點），這個差距在 {total} 位英雄中排第 {rank}（越前面越偏前期），屬於前期型英雄。",
        "stage_mid": "{name}在短局（16 分鐘內）與長局（22 分鐘以上）的勝率差為 {signed} 個百分點，在全英雄中屬於前後期平衡。",
        "dir_up": "高",
        "dir_down": "低",
        "fit_both": "陣容方面，{name}在{best}型隊伍中勝率較好（{best_pp}），在{worst}型隊伍中較差（{worst_pp}）。",
        "fit_best": "陣容方面，{name}在{best}型隊伍中勝率較好（{best_pp}）。",
        "fit_worst": "陣容方面，{name}在{worst}型隊伍中勝率較差（{worst_pp}）。",
        "pp": "{signed} 個百分點",
        "method": "數據來源為台服 Mayhem（queue 2400）實戰對局。勝率經 Bayesian 收縮調整，以降低小樣本的誤差；所有數字都是歷史對局的關聯，不是單場勝負的保證。",
        "about": "方法與資料說明",
        "about_href": "/about/",
    },
    "zh-Hans": {
        "title": "{name} 大乱斗分析",
        "sep": "、",
        "clause_sep": "；",
        "strength": "{name}在 Mayhem 大乱斗{patch}的 Bayesian 调整胜率为 {wr}，在 {total} 位英雄中排第 {rank} 名，样本 {games} 场。{verdict}",
        "patch": "（{patch} 版本）",
        "verdict_top": "属于当前版本的强势英雄。",
        "verdict_bottom": "当前版本偏弱，选到时更值得参考海克斯与出装的选择。",
        "verdict_mid": "强度处于中段。",
        "aug_good": "棱彩海克斯里，{name}表现最好的是{names}",
        "aug_bad": "{names}在{name}身上胜率偏低，三选一时可以优先避开",
        "aug_item": "{aug}（{wr}，{games} 场）",
        "aug_join": "；相对地，",
        "end": "。",
        "bad_item": "常见但胜率偏低的装备：{items}。这是历史关联，可能反映对局情境（例如落后时才补的装），不一定是装备本身不好。",
        "bad_item_one": "{item}有 {pick} 的{name}对局会出，胜率比{name}平均低 {lift} 个百分点",
        "stage_late": "比起 16 分钟内结束的短局，{name}在 22 分钟以上的长局胜率{dir} {diff} 个百分点，这个差距在 {total} 位英雄中排第 {rank}（越前面越偏后期），属于后期型英雄。",
        "stage_early": "比起 22 分钟以上的长局，{name}在 16 分钟内结束的短局胜率表现较好（长局减短局为 {signed} 个百分点），这个差距在 {total} 位英雄中排第 {rank}（越前面越偏前期），属于前期型英雄。",
        "stage_mid": "{name}在短局（16 分钟内）与长局（22 分钟以上）的胜率差为 {signed} 个百分点，在全英雄中属于前后期平衡。",
        "dir_up": "高",
        "dir_down": "低",
        "fit_both": "阵容方面，{name}在{best}型队伍中胜率较好（{best_pp}），在{worst}型队伍中较差（{worst_pp}）。",
        "fit_best": "阵容方面，{name}在{best}型队伍中胜率较好（{best_pp}）。",
        "fit_worst": "阵容方面，{name}在{worst}型队伍中胜率较差（{worst_pp}）。",
        "pp": "{signed} 个百分点",
        "method": "数据来源为台服 Mayhem（queue 2400）实战对局。胜率经 Bayesian 收缩调整，以降低小样本的误差；所有数字都是历史对局的关联，不是单场胜负的保证。",
        "about": "方法与数据说明",
        "about_href": "/zh-cn/about/",
    },
    "en": {
        "title": "{name} ARAM Mayhem analysis",
        "sep": ", ",
        "clause_sep": "; ",
        "strength": "{name} has a Bayesian-adjusted win rate of {wr} in ARAM Mayhem{patch}, ranking #{rank} of {total} champions over {games} games. {verdict}",
        "patch": " (patch {patch})",
        "verdict_top": "That puts it among the strongest picks this patch.",
        "verdict_bottom": "That is on the weak side this patch, so augment and build choices matter more.",
        "verdict_mid": "That is mid-pack this patch.",
        "aug_good": "Among prismatic augments, {name} performs best with {names}",
        "aug_bad": "{names} underperform on {name} and are worth skipping when offered",
        "aug_item": "{aug} ({wr}, {games} games)",
        "aug_join": "; by contrast, ",
        "end": ".",
        "bad_item": "Popular but low-win-rate items: {items}. This is a historical association that may reflect game state (for example items bought while behind), not proof the item itself is bad.",
        "bad_item_one": "{item} is built in {pick} of {name} games and wins {lift} points below {name}'s average",
        "stage_late": "Compared with short games (ending within 16 minutes), {name}'s win rate is {diff} points {dir} in long games (22+ minutes), ranking #{rank} of {total} for late-game lean, which makes it a late-game champion.",
        "stage_early": "{name} does better in short games (ending within 16 minutes) than in long games (22+ minutes): long minus short is {signed} points, ranking #{rank} of {total} for early-game lean, which makes it an early-game champion.",
        "stage_mid": "{name}'s win-rate gap between short games (within 16 minutes) and long games (22+ minutes) is {signed} points, balanced relative to other champions.",
        "dir_up": "higher",
        "dir_down": "lower",
        "fit_both": "On team composition, {name} wins more on {best} teams ({best_pp}) and less on {worst} teams ({worst_pp}).",
        "fit_best": "On team composition, {name} wins more on {best} teams ({best_pp}).",
        "fit_worst": "On team composition, {name} wins less on {worst} teams ({worst_pp}).",
        "pp": "{signed} pts",
        "method": "Data comes from Taiwan-server ARAM Mayhem (queue 2400) matches. Win rates use Bayesian shrinkage to reduce small-sample noise; every number is a historical association, not a guarantee of any single game.",
        "about": "Methods and data notes",
        "about_href": "/en/about/",
    },
}

_AXES: dict[tuple, dict] = {}
_FIT: dict[tuple, dict] = {}


def _cached_json(cache: dict, path: Path) -> dict:
    key = (str(path), _file_signature(path))
    hit = cache.get(key)
    if hit is not None:
        return hit
    return _bounded_cache(cache, key, _read_json_object(path))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _pp(value_pp: float) -> str:
    return f"{value_pp:+.1f}".replace("-", "−")


def _stage_rank(axes: dict, cid: str) -> dict | None:
    """Percentile of (late − early) win rate among champions, as site.js does."""
    params = axes.get("params") or {}
    min_games = int(params.get("min_games") or 0)
    rows = []
    for key, row in (axes.get("champs") or {}).items():
        if not isinstance(row, dict):
            continue
        try:
            score = float(row["late_wr"]) - float(row["early_wr"])
        except (KeyError, TypeError, ValueError):
            continue
        if int(row.get("n") or 0) < min_games:
            continue
        rows.append((score, str(key)))
    rows.sort()
    n = len(rows)
    for index, (score, key) in enumerate(rows):
        if key == cid:
            pr = 50 if n <= 1 else round(index / (n - 1) * 100)
            return {"score": score, "pr": pr, "early_rank": index + 1,
                    "late_rank": n - index, "total": n}
    return None


def _archetype_label(raw: str, language: str, t2s_table: dict) -> str:
    en, _, zh = str(raw).partition("/")
    if language == "en":
        return en.strip() or raw
    zh = zh.strip() or en.strip()
    return _t2s(zh, t2s_table) if language == "zh-Hans" else zh


def _strength(copy, name, record, patch) -> str:
    if not record or not record.get("rank") or not record.get("rank_total"):
        return ""
    rank, total = int(record["rank"]), int(record["rank_total"])
    share = rank / total
    verdict = (copy["verdict_top"] if share <= 0.2
               else copy["verdict_bottom"] if share > 0.8 else copy["verdict_mid"])
    return copy["strength"].format(
        name=name, patch=patch, wr=_pct(float(record["bayes_wr"])), total=total,
        rank=rank, games=f"{int(record['games']):,}", verdict=verdict,
    ).strip()


def _augments(copy, name, shard, language, tables) -> str:
    def entries(rows, limit):
        out = []
        for row in rows or []:
            if not isinstance(row, dict) or int(row.get("g") or 0) < _MIN_AUG_GAMES:
                continue
            aug = _augment_name(row.get("id"), language, tables)
            if not aug:
                continue
            out.append(copy["aug_item"].format(
                aug=aug, wr=_pct(float(row["wr"])), games=f"{int(row['g']):,}"))
            if len(out) >= limit:
                break
        return out

    good = entries(((shard.get("top") or {}).get("kPrismatic")), 3)
    bad = entries(((shard.get("bot") or {}).get("kPrismatic")), 2)
    parts = []
    if good:
        parts.append(copy["aug_good"].format(name=name, names=copy["sep"].join(good)))
    if bad:
        parts.append(copy["aug_bad"].format(name=name, names=copy["sep"].join(bad)))
    if not parts:
        return ""
    return copy["aug_join"].join(parts) + copy["end"]


def _bad_items(copy, name, shard, language, tables) -> str:
    rows = ((shard.get("singleItems") or {}).get("popularBad")) or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pick, lift = float(row.get("pick") or 0), float(row.get("lift") or 0) * 100
        if pick < _MIN_BAD_ITEM_PICK or -lift < _MIN_BAD_ITEM_LIFT_PP:
            continue
        items = row.get("items") or []
        item = _item_name(items[0].get("id"), language, tables) if items else ""
        if not item:
            item = str(row.get("name_en") if language == "en" else row.get("name_zh") or "")
            if language == "zh-Hans":
                item = _t2s(item, tables["t2s"])
        if not item:
            continue
        out.append(copy["bad_item_one"].format(
            item=item, pick=f"{pick * 100:.0f}%", name=name, lift=f"{-lift:.1f}"))
        if len(out) >= 2:
            break
    if not out:
        return ""
    return copy["bad_item"].format(items=copy["clause_sep"].join(out))


def _stage(copy, name, stage) -> str:
    if not stage:
        return ""
    diff_pp = stage["score"] * 100
    if stage["pr"] > _STAGE_PR_HI:
        return copy["stage_late"].format(
            name=name, dir=copy["dir_up"] if diff_pp >= 0 else copy["dir_down"],
            diff=f"{abs(diff_pp):.1f}", total=stage["total"], rank=stage["late_rank"])
    if stage["pr"] < _STAGE_PR_LO:
        return copy["stage_early"].format(
            name=name, signed=_pp(diff_pp), total=stage["total"], rank=stage["early_rank"])
    return copy["stage_mid"].format(name=name, signed=_pp(diff_pp))


def _fit(copy, name, fit_doc, cid, language, t2s_table) -> str:
    cell = ((fit_doc.get("champs") or {}).get(cid) or {}).get("fit") or {}
    labels = fit_doc.get("archetype_labels") or {}
    rows = [(float(v["delta"]), k) for k, v in cell.items()
            if isinstance(v, dict) and v.get("q") and k in labels]
    if not rows:
        return ""
    rows.sort()
    best = rows[-1] if rows[-1][0] >= _MIN_FIT_PP else None
    worst = rows[0] if rows[0][0] <= -_MIN_FIT_PP else None

    def fmt(row):
        return (_archetype_label(labels[row[1]], language, t2s_table),
                copy["pp"].format(signed=_pp(row[0])))

    if best and worst:
        (b, bp), (w, wp) = fmt(best), fmt(worst)
        return copy["fit_both"].format(name=name, best=b, best_pp=bp, worst=w, worst_pp=wp)
    if best:
        b, bp = fmt(best)
        return copy["fit_best"].format(name=name, best=b, best_pp=bp)
    if worst:
        w, wp = fmt(worst)
        return copy["fit_worst"].format(name=name, worst=w, worst_pp=wp)
    return ""


def champion_analysis(
    *, route: dict, record: dict | None, api_dir: Path | None, patch_label: str,
    languages=LANGUAGES,
) -> dict[str, dict]:
    """Return {language: {"title", "paragraphs", "about": {"label", "href"}}}.

    A language is omitted when no data paragraph could be written for it.
    """
    if api_dir is None:
        return {}
    api_dir = Path(api_dir)
    cid = str(route.get("cid") or "")
    from aram_nn.site.champion_shell import _champion_shard

    shard = _champion_shard(api_dir, cid)
    tables = _name_tables(api_dir)
    axes = _cached_json(_AXES, api_dir / "champ-empirical-axes.json")
    fit_doc = _cached_json(_FIT, api_dir / "champ-archetype-fit.json")
    stage = _stage_rank(axes, cid)
    name_keys = {"zh-Hant": "name_zh", "zh-Hans": "name_cn", "en": "name_en"}
    out: dict[str, dict] = {}
    for language in languages:
        copy = _COPY[language]
        name = str(route.get(name_keys[language]) or route.get("name_en") or "")
        bare = re.sub(r"^\s*patch\s+", "", patch_label or "", flags=re.I).strip()
        patch = copy["patch"].format(patch=bare) if bare else ""
        paragraphs = [p for p in (
            _strength(copy, name, record, patch),
            _augments(copy, name, shard, language, tables),
            _bad_items(copy, name, shard, language, tables),
            _stage(copy, name, stage),
            _fit(copy, name, fit_doc, cid, language, tables["t2s"]),
        ) if p]
        if not paragraphs:
            continue
        out[language] = {
            "title": copy["title"].format(name=name),
            "paragraphs": paragraphs + [copy["method"]],
            "about": {"label": copy["about"], "href": copy["about_href"]},
        }
    return out


def analysis_html(block: dict | None) -> str:
    """Static markup for one language's analysis block ('' when absent)."""
    if not block:
        return ""
    paras = "".join(f"<p>{html.escape(p)}</p>" for p in block["paragraphs"])
    about = block["about"]
    return (
        "<section class='view-narrow champ-analysis' id='champion-analysis'>"
        f"<h2 class='section-head'>{html.escape(block['title'])}</h2>{paras}"
        f"<p><a href='{html.escape(about['href'], quote=True)}'>{html.escape(about['label'])}</a></p>"
        "</section>"
    )
