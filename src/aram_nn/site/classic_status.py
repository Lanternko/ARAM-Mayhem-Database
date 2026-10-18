"""Public Classic retirement notice, shared by shell and routine data builds."""
from __future__ import annotations

import hashlib
import html
from pathlib import Path


CLASSIC_DISABLED_DATE = "2026-10-18"
_COPY = {
    "zh-Hant": {
        "prefix": "", "language": "語言", "theme": "切換主題",
        "status": "網站狀態", "title": "經典模式已停用",
        "description": "Classic 已停止主動收集與排行更新，本站目前專注於 Mayhem。",
        "reason": "目前將資料收集與維護資源集中於 Mayhem。Classic 已停止主動收集，無法持續維持資料時效，因此暫停展示英雄、裝備與分路排行，避免舊資料被誤認為當前版本的建議。",
        "scope": "停用範圍", "scope_text": "Classic（台服 TW・queue 4310）",
        "classic": "Classic 資料與排行", "paused": "已停用",
        "mayhem": "Mayhem 網站功能", "available": "持續提供",
        "date": "停用日期", "resume": "目前沒有恢復時間表；若有調整，會更新此頁。",
        "home": "前往 Mayhem", "feedback": "意見回饋",
        "note": "其他玩家歷史中的 Classic 對局仍可能順帶收集，但不代表 Classic 統計服務已恢復。",
    },
    "zh-Hans": {
        "prefix": "/zh-cn", "language": "语言", "theme": "切换主题",
        "status": "网站状态", "title": "经典模式已停用",
        "description": "Classic 已停止主动收集与排行更新，本站目前专注于 Mayhem。",
        "reason": "目前将数据收集与维护资源集中于 Mayhem。Classic 已停止主动收集，无法持续维持数据时效，因此暂停展示英雄、装备与分路排行，避免旧数据被误认为当前版本的建议。",
        "scope": "停用范围", "scope_text": "Classic（台服 TW・queue 4310）",
        "classic": "Classic 数据与排行", "paused": "已停用",
        "mayhem": "Mayhem 网站功能", "available": "持续提供",
        "date": "停用日期", "resume": "目前没有恢复时间表；如有调整，会更新此页。",
        "home": "前往 Mayhem", "feedback": "意见反馈",
        "note": "其他玩家历史中的 Classic 对局仍可能顺带收集，但不代表 Classic 统计服务已恢复。",
    },
    "en": {
        "prefix": "/en", "language": "Language", "theme": "Toggle theme",
        "status": "Site status", "title": "Classic is unavailable",
        "description": "Active Classic collection and ranking updates have stopped. This site now focuses on Mayhem.",
        "reason": "We are focusing data collection and maintenance on Mayhem. Active Classic collection has stopped, so we cannot keep its data current. Champion, item and position rankings are unavailable to avoid presenting old data as advice for the current patch.",
        "scope": "Scope", "scope_text": "Classic (TW region · queue 4310)",
        "classic": "Classic data and rankings", "paused": "Disabled",
        "mayhem": "Mayhem website features", "available": "Available",
        "date": "Disabled since", "resume": "There is no scheduled return. Any change will be posted here.",
        "home": "Go to Mayhem", "feedback": "Send feedback",
        "note": "Classic matches may still be collected incidentally from other players’ histories. This does not mean Classic statistics have resumed.",
    },
}


def render_classic_status(*, locale: str, site_url: str, css_version: str) -> str:
    copy = _COPY[locale]
    esc = html.escape
    origin = site_url.rstrip("/")
    prefix = copy["prefix"]
    canonical = origin + prefix + "/classic.html"
    alternatives = "".join(
        f"<link rel='alternate' hreflang='{lang}' href='{esc(origin + text['prefix'] + '/classic.html', quote=True)}'>"
        for lang, text in _COPY.items()
    )
    languages = "".join(
        f"<a href='{text['prefix']}/classic.html' lang='{lang}'"
        + (" aria-current='page'" if lang == locale else "")
        + f">{name}</a>"
        for (lang, text), name in zip(_COPY.items(), ("繁體中文", "简体中文", "English"))
    )
    font_region = "SC" if locale == "zh-Hans" else "TC"
    return f"""<!doctype html>
<html lang='{locale}'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(copy['title'])} | arammeta</title>
<meta name='description' content='{esc(copy['description'], quote=True)}'>
<meta name='robots' content='noindex,follow'>
<link rel='canonical' href='{esc(canonical, quote=True)}'>{alternatives}
<link rel='icon' href='/favicon.svg' type='image/svg+xml'>
<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Outfit:wght@500;700&amp;family=Noto+Sans+{font_region}:wght@400;500;600;700&amp;display=swap'>
<script>try{{document.documentElement.dataset.theme=localStorage.getItem('aram-mayhem-site-theme')==='light'?'light':'dark'}}catch{{}}</script>
<link rel='stylesheet' href='/assets/site.css?v={css_version}'>
</head><body class='classic-status-page'>
<header class='site-header'><div class='site-header-inner'>
<a class='brand' href='{prefix}/' aria-label='arammeta'><span class='brand-title'><span class='brand-aram'>aram</span><span class='brand-meta'>meta</span></span></a>
<div class='classic-status-controls'>
<nav class='classic-status-languages' aria-label='{copy['language']}'>{languages}</nav>
<button class='classic-status-theme' type='button' aria-label='{copy['theme']}' title='{copy['theme']}'><svg viewBox='0 0 24 24' width='18' height='18' fill='none' stroke='currentColor' stroke-width='1.6' aria-hidden='true'><circle cx='12' cy='12' r='8'/><path d='M12 4a8 8 0 0 1 0 16z' fill='currentColor'/></svg></button>
</div></div></header>
<main class='classic-status-main'>
<p class='classic-status-eyebrow'>{copy['status']}</p>
<h1>{copy['title']}</h1><p class='classic-status-reason'>{copy['reason']}</p>
<dl class='classic-status-details'>
<div><dt>{copy['scope']}</dt><dd>{copy['scope_text']}</dd></div>
<div><dt>{copy['classic']}</dt><dd>{copy['paused']}</dd></div>
<div><dt>{copy['mayhem']}</dt><dd>{copy['available']}</dd></div>
<div><dt>{copy['date']}</dt><dd><time datetime='{CLASSIC_DISABLED_DATE}'>{CLASSIC_DISABLED_DATE}</time></dd></div>
</dl>
<p class='classic-status-resume'>{copy['resume']}</p>
<div class='classic-status-actions'><a class='classic-status-primary' href='{prefix}/'>{copy['home']} <span aria-hidden='true'>→</span></a><a href='{prefix}/feedback/'>{copy['feedback']}</a></div>
<p class='classic-status-note'>{copy['note']}</p>
</main>
<script>document.querySelector('.classic-status-theme').addEventListener('click',()=>{{const theme=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=theme;try{{localStorage.setItem('aram-mayhem-site-theme',theme)}}catch{{}}}});</script>
</body></html>
"""


def write_classic_status_pages(root: Path, *, site_url: str = "https://arammeta.com") -> list[Path]:
    """Replace all existing public Classic URLs without DB/network access."""
    css = (Path(__file__).resolve().parents[3] / "scripts/templates/site.css").read_text(encoding="utf-8")
    version = hashlib.sha1(css.encode("utf-8")).hexdigest()[:12]
    written = []
    for locale, copy in _COPY.items():
        path = root / copy["prefix"].lstrip("/") / "classic.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_classic_status(locale=locale, site_url=site_url, css_version=version), encoding="utf-8")
        written.append(path)
    return written
