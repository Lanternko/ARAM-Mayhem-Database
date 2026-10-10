"""Lazy rankings retain complete evidence; quick picks never invent a build."""
import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import tierlist_render as render


def test_shards_preserve_complete_rankings_and_slots(tmp_path):
    top = {key: [dict(id=i, wr=.5, g=100, score=.02, pick=.1,
                      slots=[dict(g=20, wr=.6, rawWr=.62)], peerPick=.2)
                  for i in range(20)] for key in ('kSilver', 'kGold', 'kPrismatic')}
    original = copy.deepcopy(top)
    payload = {'champs': {'1': {'top': top, 'items': {'top': []}}}}
    render.write_champion_detail_shards(payload, payload_out_path=tmp_path / 'api/tier-list.json',
                                       payload_url='api/tier-list.json', version='test-version')
    champion = json.loads((tmp_path / 'api/champions/1.json').read_text())
    bulk = json.loads((tmp_path / 'api/champions/augment-stats.json').read_text())
    assert champion['top'] == bulk['1'] == original
    for key, rows in payload['champs']['1']['top'].items():
        assert len(rows) == 3
        assert [r['id'] for r in rows] == [r['id'] for r in original[key][:3]]
        assert all('slots' not in r for r in rows)
    assert payload['augmentStatsUrl'] == 'api/champions/augment-stats.json'
    assert render.split_champion_detail_payloads(payload) == {}


def test_full_rankings_loader_dedupes_validates_and_retries():
    js = (ROOT / 'scripts/templates/site.js').read_text(encoding='utf8')
    loader = js[js.index('    let fullAugmentStatsReady'):js.index('    // Empirical secondary artifacts')]
    script = r'''
    const assert = require('node:assert/strict');
    const DATA = {champs:{1:{top:{kGold:[{id:7}]}},2:{top:{kSilver:[]}}},augmentStatsUrl:'api/champions/augment-stats.json',detailVersion:'v1'};
    let _augChampIdx = new Map(), requests = 0, resolve, reject;
    function loadSitePayload(url) {
        assert.equal(url, 'api/champions/augment-stats.json?v=v1'); requests++;
        return new Promise((r,j)=>{resolve=r;reject=j;});
    }
    (async()=>{
        const first = ensureFullAugmentStats(), second = ensureFullAugmentStats();
        assert.equal(first,second); assert.equal(requests,1);
        resolve({'1':{kGold:[]}});
        await assert.rejects(first); assert.deepEqual(DATA.champs[1].top,{kGold:[{id:7}]});
        const retry = ensureFullAugmentStats();
        resolve({'1':{kGold:[{id:7,slots:[{wr:.6}]}]},'2':{kSilver:[]}});
        await retry; assert.equal(DATA.champs[1].top.kGold[0].slots[0].wr,.6);
        assert.equal(_augChampIdx,null);
        await ensureFullAugmentStats(); assert.equal(requests,2);
    })().catch(e=>{console.error(e);process.exitCode=1;});
    '''
    start = script.index('(async()=>')
    subprocess.run(['node', '-e', script[:start] + loader + script[start:]],
                   check=True, capture_output=True, text=True, encoding='utf8')


def test_quick_ui_uses_one_observed_completion_and_all_three_rarities():
    js = (ROOT / 'scripts/templates/site.js').read_text(encoding='utf8')
    functions = js[js.index("    const OVERVIEW_MODE_KEY"):js.index("    document.addEventListener('click', event => {", js.index('    const OVERVIEW_MODE_KEY'))]
    script = r'''
    const assert = require('node:assert/strict');
    let currentLang='zh';
    const localStorage={getItem:()=>null}, DATA={augs:{1:{icon:'aug.png',name:'增幅<測試>'}}};
    const escHtml=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
    const pickLang=(zh,en)=>currentLang==='en'?en:zh, fmtInt=String;
    const pct=n=>(n*100).toFixed(1)+'%', tr=()=>({insufficient:'No data'});
    const itemDisplayName=i=>i.name, itemIconHtml=i=>`<img src="i${i.id}.png">`;
    const itemTipSource=s=>`<template>${s}</template>`, buildItemTipHtml=()=>'', buildAugTipHtml=()=>'';
    const augName=a=>a.name, wrToneTier=()=>3;
    const six=Array.from({length:6},(_,i)=>({id:i+10,name:'Item'+i}));
    const info={itemClusters:{groups:[{core:six.slice(0,2),quickCore:{items:six.slice(0,3),g:500,wr:.58},routeBoot:{...six[5],g:300,wr:.57,pick:.6},completion:six,completionGames:25,completionBoot:15,options:[{id:999}]}]},top:{kSilver:[{id:1,wr:.6}],kGold:[{id:1,wr:.6}],kPrismatic:[{id:1,wr:.6}]}};
    let html=buildQuickOverview('1', info);
    assert.equal((html.match(/class="quick-rarity /g)||[]).length,3);
    assert(html.includes('25')); assert(!html.includes('i999.png')); assert(html.includes('增幅&lt;測試>'));
    assert(html.includes('i14.png'));
    assert(html.includes('58.0%')); assert(html.includes('核心三件')); assert(html.includes('500'));
    assert.equal((html.match(/class="quick-core-label"/g)||[]).length,3);
    info.itemClusters.groups[0].routeBoot.id=999;
    assert(!buildQuickOverview('1',info).includes('i14.png')); // never mismatch example boots
    info.itemClusters.groups[0].routeBoot.id=15;
    info.itemClusters.groups[0].completionGames=2;
    html=buildQuickOverview('1',info); assert(!html.includes('i14.png')); assert(html.includes('先參考核心組合'));
    currentLang='en'; assert(buildQuickOverview('1',info).includes('No full-build example'));
    assert(buildOverviewModes('1',info,'COMPARE').includes('data-overview-mode="compare"'));
    '''
    subprocess.run(['node', '-e', functions + script], check=True, capture_output=True, text=True, encoding='utf8')
