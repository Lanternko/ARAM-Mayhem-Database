from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AugmentPoolPickerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (ROOT / "scripts" / "templates" / "site.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "scripts" / "templates" / "site.css").read_text(encoding="utf-8")
        cls.render = (ROOT / "scripts" / "tierlist_render.py").read_text(encoding="utf-8")

    def test_champion_picker_has_role_filter_chips(self) -> None:
        self.assertIn("function apoolRoleBarHtml()", self.js)
        self.assertIn("data-apool-role", self.js)
        self.assertIn("ITEM_FILTER_ROLE_ORDER.map(chip)", self.js)
        self.assertIn("augPools.role", self.js)
        self.assertIn('trackEvent(\'aug_pool_role\'', self.js)
        self.assertIn(".apool-role-bar", self.css)
        self.assertIn(".apool-role-chip.is-active", self.css)
        for role in ("Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank"):
            self.assertIn(f".apool-role-chip.role-{role}", self.css)

    def test_role_filter_ands_with_search_and_uses_champion_tags(self) -> None:
        self.assertIn("apoolChampRoles(r.cid)", self.js)
        self.assertIn("data-roles=", self.js)
        self.assertIn("const matchRole = !role || roles.includes(role)", self.js)
        self.assertIn("btn.hidden = hide", self.js)
        self.assertIn("apool-champ-empty", self.js)
        self.assertIn("沒有符合的英雄", self.js)

    def test_pools_view_has_shareable_path(self) -> None:
        self.assertIn("function pathForRoute(view, sub)", self.js)
        self.assertIn("return prefix + '/' + view + '/pools/'", self.js)
        self.assertIn("segs[1] === 'pools'", self.js)
        self.assertIn("syncUrlToRoute('augments', augmentsSub()", self.js)
        self.assertIn("href='/augments/pools/'", self.render)
        self.assertIn("function poolsPageTitle()", self.js)
        self.assertIn("function augmentsPageTitle()", self.js)
        self.assertIn(".aug-mode-tab:visited", self.css)
