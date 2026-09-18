from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SiteSearchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (ROOT / "scripts" / "templates" / "site.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "scripts" / "templates" / "site.css").read_text(encoding="utf-8")
        cls.render = (ROOT / "scripts" / "tierlist_render.py").read_text(encoding="utf-8")

    def test_home_search_defaults_to_champions_and_has_advanced_scope(self) -> None:
        self.assertIn("scope: 'champions'", self.js)
        self.assertIn("data-search-scope='champions'", self.render)
        self.assertIn("data-search-scope='all'", self.render)
        self.assertIn("searchScopeChampionOption", self.js)
        self.assertIn("searchScopeAllOption", self.js)
        self.assertIn("searchAriaChampion", self.js)
        self.assertIn("searchAriaAll", self.js)
        self.assertIn("aria-label='搜尋範圍：英雄。展開進階搜尋'", self.render)
        self.assertIn("aria-label='只搜英雄：中／英文名稱、別名'", self.render)
        for alias in ("腎", "莫甘娜", "模乾那", "EZ", "火人", "死歌", "死哥"):
            self.assertIn(alias, self.render)

    def test_champion_scope_does_not_use_detail_item_or_augment_blob(self) -> None:
        self.assertIn("data-champion-search", self.render)
        self.assertIn("const championBlob = c.getAttribute('data-champion-search')", self.js)
        self.assertIn("allSearch ? (heroMatch || relatedMatch) : heroMatch", self.js)
        self.assertIn("related_search_index = _build_related_search_index(js_champs)", self.render)

    def test_advanced_search_has_reverse_index_and_legacy_fallback(self) -> None:
        self.assertIn('"searchIndex":', self.render)
        self.assertIn("function relatedSearchMatches(query)", self.js)
        self.assertIn("async function ensureRelatedSearchIndex()", self.js)
        self.assertIn("ensureChampDetail(cid)", self.js)
        self.assertIn("buildClientRelatedSearchIndex(DATA.champs || {})", self.js)

    def test_search_normalizes_and_tolerates_small_typos(self) -> None:
        self.assertIn("function normalizeSearchText(value)", self.js)
        self.assertIn("function searchEditDistanceWithin(a, b, limit)", self.js)
        self.assertIn("function searchTokenMatches(query, candidate)", self.js)
        self.assertIn("searchMatchesText(championBlob, q)", self.js)
        self.assertIn("NAMES_ZH_CN.t2s", self.js)
        self.assertIn("const fuzzyMinLength = isHan ? 4 : 5", self.js)
        self.assertIn("const maxEdits = 1", self.js)

    def test_search_stays_available_on_mobile(self) -> None:
        self.assertIn(".search-rail {\n            display: flex !important;", self.css)
        self.assertNotIn(".search-rail,\n        .search-wrap,", self.css)
        self.assertIn(".search-scope-option:focus-visible", self.css)
