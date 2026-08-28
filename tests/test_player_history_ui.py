from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_tier_list import resolve_player_history_api_url  # noqa: E402
from tierlist_render import render_html  # noqa: E402


def render_shell(**overrides: object) -> str:
    args: dict[str, object] = {
        "records": [],
        "champ_meta": {},
        "champ_profiles": {},
        "champ_picks": {},
        "champ_sets": {},
        "champ_item_builds": {},
        "champ_single_items": {},
        "champ_boot_items": {},
        "champ_spell_items": {},
        "champ_item_clusters": {},
        "champ_augment_types": {},
        "champ_synergy": {},
        "aug_meta": {},
        "patch_changes": {},
        "queue_id": 2400,
        "patch_prefix": "16.10",
        "ddragon_version": "15.1.1",
        "total_games": 0,
        "min_games_per_pair": 15,
        "min_synergy_games": 10,
        "player_history_api_url": "",
        "player_history_route": False,
    }
    args.update(overrides)
    return render_html(**args)  # type: ignore[arg-type]


class PlayerHistoryUiTests(unittest.TestCase):
    def test_normal_home_shell_is_isolated_and_hidden_shell_carries_api(self) -> None:
        normal = render_shell(player_history_api_url="https://history.example/")
        self.assertNotIn("player-history-panel", normal)
        self.assertNotIn("https://history.example", normal)
        self.assertNotIn("__PLAYER_HISTORY_API_BASE__", normal)

        hidden = render_shell(
            player_history_route=True,
            player_history_api_url="https://history.example/",
        )
        self.assertIn("data-route='player-history'", hidden)
        self.assertIn("data-page='player-history'", hidden)
        self.assertIn("data-player-history-api-base='https://history.example'", hidden)
        self.assertIn("id='player-history-panel'", hidden)
        self.assertIn('<meta name="robots" content="noindex,nofollow">', hidden)
        self.assertNotRegex(hidden, r"id='player-history-input'[^>]*disabled")

        disabled = render_shell(player_history_route=True, player_history_api_url="")
        self.assertIn("data-player-history-api-base=''", disabled)
        self.assertRegex(disabled, r"id='player-history-input'[^>]*disabled")
        self.assertRegex(disabled, r"id='player-history-submit'[^>]*disabled")

    def test_hidden_topology_and_locale_attributes_are_stable(self) -> None:
        html = render_shell(player_history_route=True)
        self.assertIn("id='view-home'", html)
        self.assertIn("id='player-history-panel'", html)
        self.assertIn("id='player-history-form'", html)
        self.assertIn("for='player-history-input'", html)
        self.assertIn("maxlength='128'", html)
        self.assertIn("autocomplete='off'", html)
        self.assertIn("spellcheck='false'", html)
        self.assertIn("aria-describedby='player-history-help player-history-status'", html)
        self.assertIn("aria-live='polite'", html)
        self.assertIn("data-i18n-zh-cn=", html)
        self.assertIn("data-i18n-en=", html)
        self.assertEqual(len(re.findall(r"class='nav-tab(?: |')", html)), 5)

    def test_hidden_metadata_does_not_canonicalize_home(self) -> None:
        html = render_shell(
            site_url="https://arammeta.com/",
            player_history_route=True,
            player_history_api_url="https://api.arammeta.com?x='\"&",
        )
        self.assertIn(
            "<link rel='canonical' href='https://arammeta.com/p/player-history/'>",
            html,
        )
        self.assertNotIn(
            "<link rel='canonical' href='https://arammeta.com/'>",
            html,
        )
        self.assertIn("https://api.arammeta.com?x=&#x27;&quot;&amp;", html)

    def test_resolver_never_falls_back_to_meta_pick(self) -> None:
        self.assertEqual(resolve_player_history_api_url("https://arammeta.com", ""), "")
        self.assertEqual(
            resolve_player_history_api_url("https://arammeta.com", " https://history.example/ "),
            "https://history.example",
        )

    def test_player_history_js_uses_post_and_fail_safe_dom_rendering(self) -> None:
        source = (ROOT / "scripts" / "templates" / "site.js").read_text(encoding="utf-8")
        start = source.index("const playerHistoryUi =")
        end = source.index("const metaPick =", start)
        block = source[start:end]
        self.assertIn("method: 'POST'", block)
        self.assertIn("/api/player-history/query", block)
        self.assertIn("JSON.stringify({ riot_id: riotId })", block)
        self.assertIn("new AbortController()", block)
        self.assertIn("Retry-After", block)
        self.assertIn("response.status === 400", block)
        self.assertIn("response.status === 403", block)
        self.assertIn("response.status === 429", block)
        self.assertIn("response.status === 503", block)
        self.assertIn("replaceChildren", block)
        self.assertNotIn("localStorage", block)
        self.assertNotIn("sessionStorage", block)
        self.assertNotIn("console.", block)
        self.assertNotIn("innerHTML", block)

    def test_player_history_css_has_theme_tokens_responsive_and_a11y_hooks(self) -> None:
        source = (ROOT / "scripts" / "templates" / "site.css").read_text(encoding="utf-8")
        self.assertIn(".player-history-panel", source)
        self.assertIn(".player-history-table-wrap", source)
        self.assertIn(".player-history-field input:focus-visible", source)
        self.assertIn("@media (max-width: 700px)", source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", source)


if __name__ == "__main__":
    unittest.main()
