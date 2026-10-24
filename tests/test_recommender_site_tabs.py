from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tierlist_render as render  # noqa: E402
from aram_nn.desktop import webview_host  # noqa: E402
from aram_nn.site.static_publish import DEFAULT_DOC_PATHS  # noqa: E402

FULL_SHELL = """<!doctype html><html lang="zh-Hant"><head>
<meta charset="utf-8"><title>arammeta</title>
<base href="/">
<link rel="canonical" href="https://arammeta.com/">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC">
<link rel="preload" href="api/tier-list.json" as="fetch">
<script>window.__ARAM_BUILD__ = {"v": 1};</script>
<script defer src="https://static.cloudflareinsights.com/beacon.min.js"></script>
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></script>
<script>document.write("<link rel='stylesheet' href='"+location.origin+"/assets/site.css?v=abc'>");</script>
</head><body><header>nav</header><main id="view-tier">cards</main>
<script>(function(){var s=document.createElement('script');s.src='/assets/site.js?v=abc';document.body.appendChild(s);})();</script>
</body></html>"""


class ChampionEmbedHtmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = render._champion_embed_html(FULL_SHELL)

    def test_marks_embed_mode_and_keeps_only_the_champion_host(self) -> None:
        self.assertIn("data-embed='champion'", self.html)
        self.assertIn("id='champ-page-host'", self.html)
        self.assertNotIn("<header>", self.html)
        self.assertNotIn("view-tier", self.html)
        self.assertIn("noindex", self.html)

    def test_keeps_build_config_assets_base_and_fonts(self) -> None:
        self.assertIn("__ARAM_BUILD__", self.html)
        self.assertIn('<base href="/">', self.html)
        self.assertIn("/assets/site.css?v=abc", self.html)
        self.assertIn("/assets/site.js?v=abc", self.html)
        self.assertIn("fonts.googleapis.com/css2", self.html)
        self.assertIn("api/tier-list.json", self.html)

    def test_drops_ads_analytics_and_seo_tags(self) -> None:
        self.assertNotIn("googlesyndication", self.html)
        self.assertNotIn("cloudflareinsights", self.html)
        self.assertNotIn("canonical", self.html)


class WebviewHostUrlTests(unittest.TestCase):
    def test_embed_url_carries_champion_and_tab(self) -> None:
        url = webview_host.embed_url(103, "items")
        self.assertTrue(url.startswith(webview_host.EMBED_URL))
        self.assertTrue(url.endswith("?cid=103&tab=items"))

    def test_site_url_slugs_alias_and_falls_back_to_index(self) -> None:
        self.assertTrue(webview_host.site_url("MonkeyKing").endswith("champions/monkeyking/"))
        self.assertTrue(webview_host.site_url("Kai'Sa").endswith("champions/kaisa/"))
        self.assertTrue(webview_host.site_url("").endswith("champions/"))

    def test_show_script_waits_for_embed_hook(self) -> None:
        script = webview_host.show_script(266, "augments")
        self.assertIn('window.aramEmbed.show(266, "augments")', script)
        self.assertIn("setTimeout", script)

    def test_tabs_match_the_site_embed_tabs(self) -> None:
        self.assertEqual(webview_host.TABS, ("overview", "compare", "items", "augments"))
        import recommend_gui

        site_keys = tuple(key for key, _label in recommend_gui.SITE_TABS if key != "matchup")
        self.assertEqual(site_keys, webview_host.TABS)
        js = (SCRIPTS / "templates" / "site.js").read_text(encoding="utf-8")
        self.assertIn("const EMBED_TABS = ['overview', 'compare', 'items', 'augments']", js)


class StaticPublishTests(unittest.TestCase):
    def test_embed_page_is_published(self) -> None:
        self.assertIn(Path("docs/embed/champion/index.html"), DEFAULT_DOC_PATHS)


if __name__ == "__main__":
    unittest.main()
