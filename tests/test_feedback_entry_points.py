from __future__ import annotations

import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tierlist_render import _feedback_fab_html, render_html  # noqa: E402


def render_shell() -> str:
    return render_html(  # type: ignore[arg-type]
        records=[],
        champ_meta={},
        champ_profiles={},
        champ_picks={},
        champ_sets={},
        champ_item_builds={},
        champ_single_items={},
        champ_boot_items={},
        champ_spell_items={},
        champ_item_clusters={},
        champ_augment_types={},
        champ_synergy={},
        aug_meta={},
        patch_changes={},
        queue_id=2400,
        patch_prefix="16.10",
        ddragon_version="15.1.1",
        total_games=0,
        min_games_per_pair=15,
        min_synergy_games=10,
        player_history_api_url="",
        player_history_route=False,
    )


class _AttrCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


class FeedbackEntryPointTests(unittest.TestCase):
    def test_one_localized_floating_link_replaces_view_banners(self) -> None:
        shell = render_shell()
        self.assertNotIn("feedback-cta", shell)
        self.assertNotIn("覺得哪個英雄的評級不對", shell)
        links = [a for tag, a in self._parse(shell) if tag == "a" and a.get("class") == "feedback-fab"]
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link["href"], "/feedback/")
        self.assertEqual(link["data-href-zh-cn"], "/zh-cn/feedback/")
        self.assertEqual(link["data-href-en"], "/en/feedback/")
        self.assertIn(f"id='{link['aria-labelledby']}'", shell)
        home_start = shell.index("data-view='home'")
        home_end = shell.index("</section>", home_start)
        self.assertIn("class='feedback-fab'", shell[home_start:home_end])
        self.assertNotIn("class='feedback-fab'", shell[home_end:])
        self.assertIn("許願新功能，或是回報網站的bug", shell)

    def test_footer_feedback_pill_shares_a_row_with_the_github_pill(self) -> None:
        shell = render_shell()
        row = re.search(r"<div class='footer-actions'>(.*?)</div>", shell, re.S)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("class='feedback-pill'", row.group(1))
        self.assertIn("class='gh-star'", row.group(1))
        self.assertLess(row.group(1).index("feedback-pill"), row.group(1).index("gh-star"))
        # Feedback left the legal-links row, where it read as boilerplate.
        site_links = re.search(r"<nav class='site-links'.*?</nav>", shell, re.S)
        assert site_links is not None
        self.assertNotIn("feedback", site_links.group(0))

    def test_floating_link_keeps_icon_and_localized_accessible_name(self) -> None:
        tags = self._parse(_feedback_fab_html())
        icon = next(attrs for tag, attrs in tags if tag == "svg")
        self.assertEqual(icon["aria-hidden"], "true")
        tip = next(attrs for tag, attrs in tags if tag == "span")
        self.assertEqual(tip["data-i18n-zh"], "許願新功能，或是回報網站的bug")
        self.assertTrue(tip["data-i18n-zh-cn"])
        self.assertTrue(tip["data-i18n-en"])

    @staticmethod
    def _parse(markup: str) -> list[tuple[str, dict[str, str | None]]]:
        parser = _AttrCollector()
        parser.feed(markup)
        return parser.tags


if __name__ == "__main__":
    unittest.main()
