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

import tierlist_render  # noqa: E402
from tierlist_render import _feedback_cta_html, render_html  # noqa: E402


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
    def test_every_data_view_ends_with_a_localized_feedback_prompt(self) -> None:
        shell = render_shell()
        for placement, view in (("home", "home"), ("augments", "augments"), ("changes", "changes")):
            start = shell.index(f"data-view='{view}'")
            end = shell.find("<section class='view ", start + 1)
            section = shell[start:end if end != -1 else len(shell)]
            self.assertIn(f"data-feedback-cta='{placement}'", section, placement)

        links = [a for tag, a in self._parse(shell) if tag == "a" and "feedback-cta-link" in (a.get("class") or "")]
        self.assertEqual(len(links), 3)
        for link in links:
            # Lowercase zh-cn: Pages is case-sensitive and /zh-CN/feedback/ 404s.
            self.assertEqual(link["href"], "/feedback/")
            self.assertEqual(link["data-href-zh-cn"], "/zh-cn/feedback/")
            self.assertEqual(link["data-href-en"], "/en/feedback/")
            self.assertTrue(link.get("data-i18n-en"))
            self.assertIn(f"id='{link['aria-describedby']}'", shell)

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

    def test_cta_copy_is_attribute_escaped(self) -> None:
        original = tierlist_render._FEEDBACK_CTA_COPY
        tierlist_render._FEEDBACK_CTA_COPY = {
            "probe": {"zh": "它's <b>", "zh_cn": "它's", "en": "Isn't it 'odd' & \"off\"?"},
        }
        try:
            markup = _feedback_cta_html("probe")
        finally:
            tierlist_render._FEEDBACK_CTA_COPY = original
        attrs = dict(self._parse(markup)[1][1])
        self.assertEqual(attrs["data-i18n-en"], "Isn't it 'odd' & \"off\"?")
        self.assertNotIn("<b>", markup)

    @staticmethod
    def _parse(markup: str) -> list[tuple[str, dict[str, str | None]]]:
        parser = _AttrCollector()
        parser.feed(markup)
        return parser.tags


if __name__ == "__main__":
    unittest.main()
