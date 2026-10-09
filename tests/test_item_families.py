"""Mana transformations must pool participants, never duplicate their evidence."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
import tierlist_engine as engine
import tierlist_render as render
from aram_nn.site.item_families import STACKED_ITEM_IDS, item_families_payload


@pytest.fixture
def item_meta():
    return {
        iid: {"id": iid, "name": f"Item {iid}", "name_zh": f"道具 {iid}",
              "name_en": f"Item {iid}", "price_total": 2900, "categories": ["Mana"],
              "icon": f"https://ddragon.leagueoflegends.com/cdn/16.19.1/img/item/{iid}.png"}
        for iid in (*STACKED_ITEM_IDS, *STACKED_ITEM_IDS.values(), 3100, 3101, 3102, 3103, 3104)
    }


@pytest.mark.parametrize("base,stacked", STACKED_ITEM_IDS.items())
def test_selectors_pool_and_dedupe_without_losing_order(base, stacked, item_meta):
    inventory = [0, "bad", base, stacked, base, 3100, 3101]
    assert engine._participant_core_item_ids(inventory, item_meta) == [stacked, 3100]
    assert engine._participant_recommendable_item_ids(inventory, item_meta) == [stacked, 3100, 3101]
    assert engine._participant_route_item_ids(inventory, item_meta) == [stacked, 3100, 3101]
    # Partial metadata must preserve a valid base item instead of dropping it.
    partial = {k: v for k, v in item_meta.items() if k != stacked}
    assert engine._participant_core_item_ids([base, 3100], partial) == [base, 3100]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "games.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE games (queue_id INT, patch TEXT, blue_wins INT, participants_json TEXT)")
        for i in range(120):
            inventory = ([3003], [3040], [3003, 3040])[i % 3] + [3100, 3101, 3102, 3103, 3104]
            participant = {"championId": 1, "teamId": 100,
                           "items" if i % 2 else "itemSlots": inventory}
            con.execute("INSERT INTO games VALUES (?,?,?,?)", (2400, "16.18.1", int(i < 80), json.dumps([participant])))
        # Other queues and patches must not enter either numerator or denominator.
        for queue, patch_name in [(450, "16.18.1"), (2400, "16.19.1")]:
            con.execute("INSERT INTO games VALUES (?,?,?,?)", (queue, patch_name, 1, json.dumps([participant])))
    return path


def test_single_pair_and_build_share_pooled_evidence(db, item_meta):
    records = [{"champion_id": 1, "raw_wr": 0.5}]
    singles = engine.compute_champ_single_item_affinities(db, 2400, "16.18", item_meta, records, min_games=1)
    rows = singles[1]["top"]
    mana = [r for r in rows if r["items"][0]["id"] in (3003, 3040)]
    assert len(mana) == 1
    assert mana[0]["items"][0]["id"] == 3040
    assert mana[0]["games"] == 120
    assert mana[0]["raw_wr"] == pytest.approx(80 / 120)
    assert mana[0]["pick_rate"] == 1
    pairs = engine.compute_champ_item_pair_affinities(db, 2400, "16.18", item_meta, records, min_games=1)[1]["top"]
    assert len(pairs) == 1
    assert [i["id"] for i in pairs[0]["items"]] == [3040, 3100]
    assert pairs[0]["games"] == 120
    builds = engine.compute_champ_item_build_clusters(db, 2400, "16.18", item_meta, records, singles)[1]["groups"]
    assert len(builds) == 1
    assert [i["id"] for i in builds[0]["core_items"]] == [3040, 3100]
    assert builds[0]["games"] == 120


def test_old_snapshot_is_rescanned_and_each_player_counts_once(db, item_meta, tmp_path):
    kwargs = dict(snapshot_dir=tmp_path / "snapshots")
    records = [{"champion_id": 1, "raw_wr": 0.5}]
    with patch.object(engine, "canonical_item_id", side_effect=lambda iid, meta: iid):
        old = engine.settled_core_item_patch_stats(db, 2400, "16.18", item_meta, records, **kwargs)
    assert old["item"][3003]["games"] == 80
    with patch.object(engine, "_scan_core_item_counters", wraps=engine._scan_core_item_counters) as scan:
        pooled = engine.settled_core_item_patch_stats(db, 2400, "16.18", item_meta, records, **kwargs)
        scan.assert_called_once()
    assert 3003 not in pooled["item"]
    assert pooled["item"][3040] == {"games": 120, "wins": 80}
    assert pooled["champ_item"][(1, 3040)]["games"] == 120
    assert pooled["champ_games"][1] == 120
    with patch.object(engine, "_scan_core_item_counters", side_effect=AssertionError("fresh cache rescanned")):
        assert engine.settled_core_item_patch_stats(db, 2400, "16.18", item_meta, records, **kwargs)["item"] == pooled["item"]


def test_compacted_payload_keeps_family_icon_and_names(item_meta):
    payload = {"champs": {"1": {"singleItems": {"top": [{"items": [dict(item_meta[3040])]}]}}}}
    with patch.object(render, "load_item_metadata", return_value=item_meta):
        render._dedupe_item_objects(payload)
    assert payload["itemFamilies"] == item_families_payload(item_meta)
    assert payload["itemFamilies"]["3040"]["id"] == 3003
    assert payload["itemFamilies"]["3040"]["icon"] == item_meta[3003]["icon"]
    assert payload["champs"]["1"]["singleItems"]["top"][0]["items"][0]["ic"] == 1


def test_frontend_explains_grouping_in_three_locales_and_keeps_legacy_payloads_honest():
    # Ordinary shells remove everything after the player-history section.
    css = render._retire_player_history_css(render._read_site_template("site.css"))
    assert ".item-family-icon > .item-family-base" in css
    source = (ROOT / "scripts/templates/site.js").read_text(encoding="utf-8")
    functions = source[source.index("    function itemFamilyBase("):source.index("    let _itemFloatTipEl")]
    script = r'''
        const assert = require('node:assert/strict');
        const DATA = {};
        let currentLang = 'zh';
        const escHtml = s => String(s).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        const itemIconUrl = item => item.icon || '';
        const itemDisplayName = item => currentLang === 'en' ? item.name_en : item.name_zh;
        const tr = () => ({});
        const itemGold = () => 0;
        const itemDescription = () => '';
        const stacked = {id:3040, icon:'stacked.png', name_zh:'熾天使', name_en:'Seraph'};
        assert(!itemIconHtml(stacked).includes('item-family-base'));
        DATA.itemFamilies = {'3040': {id:3003, icon:'base.png', name_zh:'大天使<測試>', name_en:'Archangel'}};
        for (const [locale, expected] of [['zh','疊滿前後合併統計'], ['zh-CN','叠满前后合并统计'], ['en','combined pre-stack and fully stacked stats']]) {
            currentLang = locale;
            const icon = itemIconHtml(stacked, 'cg-core-icon');
            assert(icon.includes('base.png') && icon.includes('stacked.png'));
            assert(icon.includes('aria-label=') && icon.includes(expected));
            assert(!icon.includes('<測試>'));
            const tip = buildItemTipHtml({name:'mana', items:[stacked], wr:'55%', games:1200});
            assert(tip.includes(expected) && tip.includes('1200'));
        }
        assert(!itemIconHtml({id:3100, icon:'ordinary.png'}).includes('item-family-base'));
    '''
    subprocess.run(["node", "-e", functions + script], check=True, capture_output=True, text=True, encoding="utf-8")
