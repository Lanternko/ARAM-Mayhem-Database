"""Champion-page augment tiles: hover tip for mouse, detail row for click/touch; tips never steal pointer input."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE_JS = ROOT / "scripts/templates/site.js"
SITE_CSS = ROOT / "scripts/templates/site.css"


def _js():
    return SITE_JS.read_text(encoding="utf-8")


def _css():
    return SITE_CSS.read_text(encoding="utf-8")


def _between(text, start, end):
    assert start in text, start
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


def test_hover_handler_skips_touch_and_open_tiles_and_respects_pinning():
    hover = _between(
        _js(),
        "// Rich item float-tip: show on hover/focus of any .has-item-tip host.",
        "document.addEventListener('mouseout'",
    )
    assert "if (_itemFloatTipPinned || _itemFloatTipDismissed) return;" in hover
    skip = "if (host.matches('.aug-tile') && (_augTouchInput || host.getAttribute('aria-expanded') === 'true')) return;"
    assert skip in hover
    # The skip must happen before the tip is shown.
    assert hover.index(skip) < hover.index("showItemFloatTip(host)")


def test_tile_focus_only_shows_tip_for_keyboard_focus():
    js = _js()
    start = js.index("host.matches('.aug-tile') && !host.matches(':focus-visible')")
    focus = js[start:js.index("document.addEventListener('focusout'", start)]
    assert "host.matches('.aug-tile') && !host.matches(':focus-visible')" in focus


def test_click_handler_pins_and_hide_unpins():
    js = _js()
    anchor = js.index("const familyHost = ev.target.closest")
    start = js.rindex("document.addEventListener('click', ev => {", 0, anchor)
    click = js[start:js.index("document.addEventListener('keydown'", anchor)]
    assert "_itemFloatTipPinned = true;" in click
    assert "ensureItemFloatTip().classList.add('is-pinned');" in click
    hide = _between(js, "function hideItemFloatTip()", "function positionItemFloatTip")
    assert "'is-pinned'" in hide
    assert "_itemFloatTipPinned = false;" in hide


def test_visible_tip_is_click_through_unless_pinned():
    css = _css()
    base = re.search(r"\.item-float-tip\.is-visible\s*\{([^}]*)\}", css)
    assert base, "missing .item-float-tip.is-visible rule"
    assert "pointer-events: none;" in base.group(1)
    pinned = re.search(r"\.item-float-tip\.is-visible\.is-pinned\s*\{([^}]*)\}", css)
    assert pinned, "missing .item-float-tip.is-visible.is-pinned rule"
    assert "pointer-events: auto;" in pinned.group(1)


def test_no_other_visible_tip_rule_reenables_pointer_events():
    css = _css()
    for selector, body in re.findall(r"([^{}]*\.item-float-tip[^{}]*)\{([^{}]*)\}", css):
        if "is-visible" not in selector or "pointer-events" not in body:
            continue
        if re.search(r"pointer-events:\s*auto", body):
            assert "is-pinned" in selector, selector.strip()


def test_augment_hosts_show_pointer_cursor():
    css = _css()
    rule = re.search(r"\.aug-tile\s*\{([^}]*)\}", css)
    assert rule, "missing .aug-tile rule"
    assert "cursor: pointer;" in rule.group(1)


def test_champion_augments_render_as_tile_grid_with_detail_row():
    js, css = _js(), _css()
    for gone in ("aug-pick-card", "aug-pick-cards", "aug-tier-row", "aug-tier-head", "champAugSort === 'rank' && i < CHAMP_AUG_PODIUM"):
        assert gone not in js, gone
    for gone in (".aug-tier-seg-btn.is-active::before", "--grain", "--plate"):
        assert gone not in css, gone
    assert 'aria-expanded="false"' in _between(js, "function buildChampAugTable", "function syncChampAugTable")
    assert "aria-expanded" in _between(js, "function toggleAugTile", "const augGridObserver")
    grid = re.search(r"\.aug-tile-grid\s*\{([^}]*)\}", css)
    assert grid and "/ 8 - 0.5px)), 1fr))" in grid.group(1), "at most 8 columns"
    detail = re.search(r"\.aug-detail\s*\{([^}]*)\}", css)
    assert detail and "grid-column: 1 / -1;" in detail.group(1)


def test_top_three_of_every_sort_are_featured_and_the_rest_fade():
    js, css = _js(), _css()
    assert "tile.classList.toggle('is-podium', i < CHAMP_AUG_PODIUM);" in js
    assert ".aug-tile-grid.is-wide-podium .aug-tile.is-podium { grid-column: span 2; }" in css
    fade = re.search(r"\.aug-tile:not\(\.is-podium\):not\(\.is-good\) \.aug-tile-icon\s*\{([^}]*)\}", css)
    assert fade and "opacity: .6;" in fade.group(1)


def test_tile_tip_sits_beside_the_tile_and_stays_in_view():
    js = _js()
    pos = _between(js, "function positionAugTileTip", "function positionItemFloatTip")
    assert "'right'" in pos and "'left'" in pos
    assert ".aug-tier-top" in pos and ".aug-tile-sorts" in pos
    assert "if (anchor.matches('.aug-tile')) {" in _between(js, "function positionItemFloatTip", "function showItemFloatTip")


def test_tile_click_is_routed_to_detail_row_not_tip_pin():
    js = _js()
    assert "toggleAugTile(augTile);" in js
    # role=button hosts are skipped by the tip-pinning click handler.
    assert "closest('button, a, [role=\"button\"]')" in js
