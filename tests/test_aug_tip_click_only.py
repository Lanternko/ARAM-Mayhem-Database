"""Augment tips open on click only and never steal pointer input while hovered."""
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


def test_hover_handler_skips_augment_hosts_and_respects_pinning():
    hover = _between(
        _js(),
        "// Rich item float-tip: show on hover/focus of any .has-item-tip host.",
        "document.addEventListener('mouseout'",
    )
    assert "if (_itemFloatTipPinned || _itemFloatTipDismissed) return;" in hover
    assert "if (host.matches('.aug-pick-card, .aug-tier-row')) return;" in hover
    # The skip must happen before the tip is shown.
    assert hover.index("host.matches('.aug-pick-card, .aug-tier-row')") < hover.index(
        "showItemFloatTip(host)"
    )


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
    rule = re.search(r"\.aug-pick-card\.has-item-tip,\s*\.aug-tier-row\.has-item-tip\s*\{([^}]*)\}", css)
    assert rule, "missing aug cursor rule"
    assert "cursor: pointer;" in rule.group(1)
