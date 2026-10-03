"""Small, indexable champion pages sharing one cacheable interactive shell."""
from __future__ import annotations

import html
import json
import re


def champion_shell_html(
    localized_html: str, *, route: dict, record: dict, language: str,
    shell_url: str, loader_url: str,
) -> str:
    """Keep the route's head and meaningful snapshot text, without redirecting."""
    match = re.search(r"window\.__ARAM_BUILD__=(.*?);?</script>", localized_html, flags=re.S)
    snapshot = json.loads(match[1]) if match else {}
    names = {"zh-Hant": "name_zh", "zh-Hans": "name_cn", "en": "name_en"}
    copy = {
        "zh-Hant": ("Bayesian 調整勝率", "英雄樣本數", "版本", "台服 · Mayhem · queue 2400", "正在載入增幅與出裝…", "查看英雄榜", "混合上版", "歷史對局關聯，非單場勝負保證。"),
        "zh-Hans": ("Bayesian 调整胜率", "英雄样本数", "版本", "台服 · Mayhem · queue 2400", "正在载入海克斯与出装…", "查看英雄榜", "混合上版", "历史对局关联，非单场胜负保证。"),
        "en": ("Bayesian win rate", "Champion sample", "Patch", "TW · Mayhem · queue 2400", "Loading augments and build…", "Champion tier list", "Previous-patch blend", "Historical association, not a guarantee of a match outcome."),
    }[language]
    name = html.escape(route[names[language]])
    prefix = {"zh-Hant": "", "zh-Hans": "/zh-cn", "en": "/en"}[language]
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
        f"<p class='section-sub'>{copy[3]} · {copy[2]}: {html.escape(snapshot.get('patchLabel', ''))}</p>"
        f"<p class='section-sub'>{html.escape(snapshot.get('buildDate', ''))} · {copy[7]}</p>"
        f"<p class='section-sub' id='champion-load-status' role='status'>{copy[4]}</p>"
        f"<a href='{prefix}/'>{copy[5]}</a></div></main>"
    )
    head = re.split(r"<body\b[^>]*>", localized_html, maxsplit=1, flags=re.I)[0]
    return (
        head + "<body>" + fallback
        + f"<script src='{html.escape(loader_url, quote=True)}' "
        + f"data-app-shell='{html.escape(shell_url, quote=True)}' defer></script>"
        + "</body></html>\n"
    )
