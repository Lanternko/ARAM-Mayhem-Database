from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from aram_nn.site.column import column_pages, load_articles  # noqa: E402
from aram_nn.site.route_intro import (  # noqa: E402
    ROUTE_INTROS,
    localize_route_intro,
    route_intro_html,
    route_intro_key,
)
from aram_nn.site.search_discovery import write_search_discovery  # noqa: E402
from tierlist_render import write_site_info_pages  # noqa: E402


def _visible_text(page: str) -> str:
    page = re.sub(r"<(script|style|svg)\b.*?</\1>", "", page, flags=re.S)
    return re.sub(r"<[^>]+>", "", page)


def _write_api(api: Path) -> None:
    api.mkdir(parents=True)
    champs = {
        str(cid): {
            "name": f"英雄{cid}", "name_zh": f"英雄{cid}", "name_en": f"Champ{cid}",
            "wr": 0.44 + cid * 0.005, "g": 5000,
        }
        for cid in range(1, 21)
    }
    champs["1"].update(name="關", name_zh="關")
    (api / "tier-list.json").write_text(
        json.dumps({"champs": champs}, ensure_ascii=False), encoding="utf-8",
    )
    (api / "names-zh-cn.json").write_text(json.dumps({
        "champs": {"1": "关羽"}, "t2s": {"關": "关", "鍵": "键", "專": "专", "欄": "栏"},
    }, ensure_ascii=False), encoding="utf-8")
    (api / "champ-empirical-axes.json").write_text(json.dumps({
        "champs": {
            str(cid): {"scaling": (cid - 10) / 100, "snowball": 1.5 + cid / 10}
            for cid in range(1, 21)
        },
    }), encoding="utf-8")


class ColumnPagesTest(unittest.TestCase):
    def test_shipped_articles_have_both_languages_and_real_length(self) -> None:
        articles = load_articles()
        self.assertGreaterEqual(len(articles), 5)
        for article in articles:
            self.assertRegex(article["date"], r"^\d{4}-\d{2}-\d{2}$")
            # Thin articles are what this feature exists to avoid.
            self.assertGreater(len(_visible_text(article["body_zh"])), 600, article["id"])
            self.assertGreater(len(_visible_text(article["body_en"])), 1500, article["id"])
            for body in (article["body_zh"], article["body_en"]):
                self.assertNotIn("<script", body)
                self.assertNotIn("games.db", body)

    def test_pages_cover_every_locale_with_reciprocal_alternates(self) -> None:
        pages = column_pages(site_url="https://arammeta.com/")
        articles = load_articles()
        self.assertEqual(len(pages), 3 * (len(articles) + 1))
        by_path = {page.canonical_path: page for page in pages}
        self.assertIn("/column/", by_path)
        for article in articles:
            for prefix, lang in (("", "zh-Hant"), ("/en", "en"), ("/zh-cn", "zh-Hans")):
                page = by_path[f"{prefix}/column/{article['id']}/"]
                self.assertEqual(page.html_lang, lang)
                self.assertEqual(page.updated, article["date"])
                for alt in ("", "/en", "/zh-cn"):
                    self.assertIn(
                        f"href='https://arammeta.com{alt}/column/{article['id']}/'",
                        page.head_extra_html,
                    )
                self.assertIn('"@type": "Article"', page.head_extra_html)
        self.assertIn("href='/en/column/how-to-read/'", by_path["/en/column/"].body_html)

    def test_zh_hans_swaps_champion_names_only_in_name_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = root / "api"
            _write_api(api)
            content = root / "content"
            content.mkdir()
            (content / "articles.json").write_text(json.dumps([{
                "id": "demo", "date": "2026-07-01", "title_zh": "專欄", "title_en": "Demo",
            }], ensure_ascii=False), encoding="utf-8")
            (content / "demo.zh.html").write_text(
                '<div class="nm">關</div><p>關鍵在 <b>關</b></p>'
                '<div class="scatter-wrap"><div class="scatter-chart" id="scatter-host"></div>'
                '<div class="scatter-legend"></div></div>',
                encoding="utf-8",
            )
            (content / "demo.en.html").write_text("<p>Demo</p>", encoding="utf-8")
            pages = {
                page.canonical_path: page
                for page in column_pages(api_dir=api, content_dir=content)
            }
            cn = pages["/zh-cn/column/demo/"].body_html
            self.assertIn('class="nm">关羽<', cn)
            self.assertIn("<b>关羽</b>", cn)
            self.assertIn("关键在", cn)  # ordinary word: char table only
            self.assertEqual(pages["/zh-cn/column/demo/"].title, "专栏")
            # The chart is static SVG drawn from the published snapshot.
            zh = pages["/column/demo/"].body_html
            self.assertIn("<svg", zh)
            self.assertEqual(zh.count("<circle"), 20)
            self.assertNotIn("scatter-host", zh)

    def test_info_pages_write_articles_and_keep_ads_off_utility_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.html"
            index.write_text(
                "<!doctype html><html><head><link rel='canonical' "
                "href='https://arammeta.com/'></head><body>home</body></html>",
                encoding="utf-8",
            )
            written = write_site_info_pages(
                index, site_url="https://arammeta.com/", build_date="2026-11-19",
            )
            article = (root / "column" / "how-to-read" / "index.html").read_text(encoding="utf-8")
            self.assertIn("rel='canonical' href='https://arammeta.com/column/how-to-read/'", article)
            self.assertIn("ca-pub-8593280194977470", article)
            self.assertIn("發布日期：2026-06-27", article)
            self.assertIn("aria-current='page'>專欄</a>", article)
            self.assertGreater(len(_visible_text(article)), 1200)
            en = (root / "en" / "column" / "how-to-read" / "index.html").read_text(encoding="utf-8")
            self.assertIn("<html lang='en'>", en)
            self.assertIn("Published: 2026-06-27", en)
            # Ad code sits next to publisher content only.
            for utility in ("privacy", "feedback", "en/feedback", "zh-cn/feedback"):
                page = (root / utility / "index.html").read_text(encoding="utf-8")
                self.assertNotIn("adsbygoogle", page, utility)
            self.assertIn("adsbygoogle", (root / "about" / "index.html").read_text(encoding="utf-8"))

            write_search_discovery(root, [index, *written], site_url="https://arammeta.com/")
            sitemap = (root / "sitemap.xml").read_text(encoding="utf-8")
            for path in ("/column/", "/column/skill-scaling/", "/en/column/skill-scaling/",
                         "/zh-cn/column/skill-scaling/"):
                self.assertIn(f"<loc>https://arammeta.com{path}</loc>", sitemap)


class RouteIntroTest(unittest.TestCase):
    def test_keys_follow_the_canonical_path(self) -> None:
        self.assertEqual(route_intro_key("/"), "home")
        self.assertEqual(route_intro_key("/en/"), "home")
        self.assertEqual(route_intro_key("/zh-cn/augments/"), "augments")
        self.assertEqual(route_intro_key("/en/augments/pools/"), "pools")
        self.assertEqual(route_intro_key("/champions/"), "champions")
        self.assertEqual(route_intro_key("/champions/ahri/"), "")

    def test_every_route_has_three_languages_of_equal_shape(self) -> None:
        for key, copy in ROUTE_INTROS.items():
            self.assertEqual(set(copy), {"zh", "zh-CN", "en"}, key)
            self.assertEqual(len({len(paragraphs) for _, paragraphs in copy.values()}), 1, key)

    def test_localized_shell_shows_one_route_in_its_language(self) -> None:
        shell = "<main>" + route_intro_html() + "</main>"
        self.assertEqual(shell.count(" hidden>"), len(ROUTE_INTROS) - 1)
        en = localize_route_intro(shell, canonical_path="/en/draft/", html_lang="en")
        self.assertIn("<section data-route-intro='draft'><details><summary><h2", en)
        self.assertNotIn("<details open", en)
        self.assertIn(">About draft analysis</h2>", en)
        self.assertIn("<section data-route-intro='home' hidden>", en)
        self.assertNotIn(">關於選角分析</h2>", en)
        champ = localize_route_intro(shell, canonical_path="/champions/ahri/", html_lang="zh-Hant")
        self.assertEqual(champ.count(" hidden>"), len(ROUTE_INTROS))
        intros = {
            key: localize_route_intro(shell, canonical_path=path, html_lang="zh-Hant")
            for key, path in (("augments", "/augments/"), ("changes", "/changes/"))
        }
        self.assertNotEqual(intros["augments"], intros["changes"])


if __name__ == "__main__":
    unittest.main()
