from __future__ import annotations

from pathlib import Path
import ast
import json
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ChampionSearchBehaviorTests(unittest.TestCase):
    """Run the shipped JS matcher against the full public champion catalog."""

    def test_nicknames_typos_and_false_positives(self) -> None:
        if not shutil.which("node"):
            self.skipTest("Node.js is required to execute the browser search matcher")
        source = (ROOT / "scripts/templates/site.js").read_text(encoding="utf-8")
        matcher = source[source.index("    function normalizeSearchText("):source.index("    function entrySearchText(")]
        tree = ast.parse((ROOT / "scripts/tierlist_render.py").read_text(encoding="utf-8"))
        # Exercise the real first-paint index builder without starting analytics.
        nodes = [n for n in ast.walk(tree) if (
            isinstance(n, ast.FunctionDef) and n.name in {"_add_search_terms", "_champ_search_blob"}
        ) or (
            isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            and n.target.id == "_CHAMPION_SEARCH_ALIASES"
        )]
        namespace: dict = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "search-index", "exec"), namespace)
        champs = json.loads((ROOT / "docs/api/tier-list.json").read_text(encoding="utf-8"))["champs"]
        names = json.loads((ROOT / "docs/api/names-zh-cn.json").read_text(encoding="utf-8"))
        aliases = namespace["_CHAMPION_SEARCH_ALIASES"]
        self.assertFalse(set(aliases) - {c["alias"].lower() for c in champs.values()})
        blobs = {
            c["alias"]: namespace["_champ_search_blob"](int(cid), c["name"], c, [])
            for cid, c in champs.items()
        }
        script = "const assert = require('node:assert/strict');\n"
        script += "const NAMES_ZH_CN = " + json.dumps(names, ensure_ascii=False) + ";\n"
        script += matcher
        script += "const blobs = " + json.dumps(blobs, ensure_ascii=False) + ";\n"
        script += r"""
function find(q) {
    const entries = Object.entries(blobs);
    const exact = entries.filter(([, blob]) => searchHasExactToken(blob, q));
    return (exact.length ? exact : entries.filter(([, blob]) => searchMatchesText(blob, q)))
        .map(([alias]) => alias).sort();
}
const cases = {
    '刀妹': 'Irelia', '剪刀妹': 'Gwen', '伊瑞莉雅': 'Irelia',
    '劍魔': 'Aatrox', '剑魔': 'Aatrox', '女警': 'Caitlyn',
    '狗頭': 'Nasus', '狗头': 'Nasus', '貓咪': 'Yuumi', '猫咪': 'Yuumi',
    '月男': 'Aphelios', '死哥': 'Karthus', '魔甘娜': 'Morgana',
    '模乾那': 'Morgana', '牙宿': 'Yasuo', '腎': 'Shen',
    'ｅｚ': 'Ezreal', 'EZ': 'Ezreal', 'j4': 'JarvanIV',
    'ireila': 'Irelia', 'ireliaa': 'Irelia', 'irela': 'Irelia',
    'irelix': 'Irelia', 'morgnaa': 'Morgana', '伊瑞莉亞': 'Irelia',
};
for (const [q, expected] of Object.entries(cases)) {
    assert.deepEqual(find(q), [expected], `query: ${q}`);
}
assert(searchMatchesText(blobs.Kaisa, "Kai'Sa"));
assert(searchMatchesText(blobs.MonkeyKing, 'wukong'));
assert(searchMatchesText(blobs.Irelia, 'irel'));
assert(searchMatchesText(blobs.Shen, 'she'));
assert(!searchMatchesText(blobs.Twitch, 'she'));
assert(!searchTokenMatches('拉克', '札克'));
assert(!searchTokenMatches('lux', 'lulu'));
assert.deepEqual(find('zzzzzzzzzz'), []);
assert(!searchMatchesText(blobs.Irelia, ''));
assert(!searchMatchesText(blobs.Irelia, '   '));
assert(!searchMatchesText(blobs.Irelia, '!!!'));
assert.equal(searchEditDistanceWithin('ireila', 'irelia', 1), 1);
assert.equal(searchEditDistanceWithin('irelia', 'irelia', 1), 0);
assert(searchEditDistanceWithin('abcdef', 'badcef', 1) > 1);
"""
        result = subprocess.run(["node", "-"], input=script, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class SiteSearchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (ROOT / "scripts" / "templates" / "site.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "scripts" / "templates" / "site.css").read_text(encoding="utf-8")
        cls.render = (ROOT / "scripts" / "tierlist_render.py").read_text(encoding="utf-8")

    def test_home_search_defaults_to_champions_and_has_advanced_scope(self) -> None:
        self.assertIn("scope: readSavedSearchScope()", self.js)
        self.assertIn("=== 'all' ? 'all' : 'champions'", self.js)
        self.assertIn("data-search-scope='champions'", self.render)
        self.assertIn("data-search-scope='all'", self.render)
        self.assertIn("searchScopeChampionOption", self.js)
        self.assertIn("searchScopeAllOption", self.js)
        self.assertIn("searchAriaChampion", self.js)
        self.assertIn("searchAriaAll", self.js)
        self.assertIn("aria-label='搜尋範圍：英雄。展開進階搜尋'", self.render)
        self.assertIn("aria-label='只搜英雄：中／英文名稱、別名'", self.render)
        self.assertIn("data-i18n-zh='全部' data-i18n-en='ALL'>全部</span>", self.render)
        self.assertIn("data-i18n-zh='英雄＋增幅＋裝備' data-i18n-en='champions + augments + items'>英雄＋增幅＋裝備</small>", self.render)
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
