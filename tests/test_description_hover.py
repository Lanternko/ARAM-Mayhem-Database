"""Description formatting preserves game text and never accepts source markup."""
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_game_descriptions_keep_structure_semantics_and_escape_source():
    js = (ROOT / "scripts/templates/site.js").read_text(encoding="utf-8")
    formatter = js[js.index("    const DESCRIPTION_TERMS"):js.index("    function buildItemTipHtml")]
    script = r'''
    const assert = require('node:assert/strict');
    const escHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    ''' + formatter + r'''
    const item = formatItemDescHtml('80 Ability Power\r\n600 Mana\r\n20 Ability Haste\r\n\r\nBaleful Blaze\r\nDeal bonus magic damage for 3 seconds.');
    assert.equal((item.match(/item-tip-section/g) || []).length, 2);
    assert.match(item, /item-tip-effect-name">Baleful Blaze</);
    assert.match(item, /desc-ap">Ability Power</);
    assert.match(item, /desc-mana">Mana</);
    assert.match(item, /desc-haste">Ability Haste</);
    assert.match(item, /desc-magic">magic damage</);
    assert.match(item, /desc-value">80</);
    for (const text of ['獲得30%魔法攻擊與技能加速，治療並賦予護盾。', '获得30%法术强度与技能急速，治疗并赋予护盾。']) {
        const out = formatItemDescHtml(text);
        for (const tone of ['ap', 'haste', 'health', 'shield']) assert.ok(out.includes('desc-' + tone));
        assert.match(out, /desc-value">30%</);
    }
    const unsafe = formatItemDescHtml('<img src=x onerror=alert(1)> & "Magic"');
    assert.ok(!unsafe.includes('<img'));
    assert.ok(unsafe.includes('&lt;img'));
    assert.ok(unsafe.includes('&amp;'));
    assert.ok(unsafe.includes('&quot;'));
    assert.equal(formatItemDescHtml(''), '');
    assert.ok(!formatItemDescHtml('Cooldown: [value]').includes('desc-value'));
    assert.ok(!formatItemDescHtml('Manaflow and healthy shieldbearers').includes('desc-tone'));
    // Repeated calls cannot inherit a shared global RegExp lastIndex.
    assert.equal(formatItemDescHtml('4% Ability Power'), formatItemDescHtml('4% Ability Power'));
    '''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
