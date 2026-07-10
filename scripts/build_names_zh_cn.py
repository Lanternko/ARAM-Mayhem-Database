# -*- coding: utf-8 -*-
"""Build docs/api/names-zh-cn.json — official CN champ/item names + compact t2s map."""
import json, re, urllib.request
from pathlib import Path
import zhconv

ROOT = Path('.')
tier = json.loads((ROOT / 'docs/api/tier-list.json').read_text(encoding='utf-8'))
site_js = (ROOT / 'scripts/templates/site.js').read_text(encoding='utf-8')
render_py = (ROOT / 'scripts/tierlist_render.py').read_text(encoding='utf-8')
blob = site_js + render_py + json.dumps(tier, ensure_ascii=False)
cjk = set(re.findall(r'[\u3400-\u9fff\uf900-\ufaff]', blob))
char_map = {}
for ch in cjk:
    conv = zhconv.convert(ch, 'zh-cn')
    if conv != ch:
        char_map[ch] = conv

ver = tier.get('ddv') or '16.13.1'

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'arammeta-build/1.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

champs_cn = fetch(f'https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/champion.json')
items_cn = fetch(f'https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/item.json')

# zh_CN ddragon swaps name/title vs EN/TW: title is the short call-name (亚索).
champ_by_id = {}
for alias, row in champs_cn['data'].items():
    cid = str(row['key'])
    # Prefer title (call name); fall back to name.
    champ_by_id[cid] = (row.get('title') or row.get('name') or '').strip()

item_names = {}
item_descs = {}
for iid in (tier.get('itemLut') or {}):
    row = items_cn['data'].get(str(iid))
    if not row:
        continue
    item_names[str(iid)] = row.get('name') or ''
    desc = row.get('description') or ''
    desc = re.sub(r'<br\s*/?>', '\n', desc, flags=re.I)
    desc = re.sub(r'<[^>]+>', '', desc)
    item_descs[str(iid)] = desc

# Pre-convert augment names/descs for better quality (phrase-aware via zhconv)
augs = {}
for aid, a in (tier.get('augs') or {}).items():
    augs[str(aid)] = {
        'n': zhconv.convert(a.get('name_zh') or a.get('name') or '', 'zh-cn'),
        'd': zhconv.convert(a.get('desc_zh') or a.get('desc') or '', 'zh-cn'),
        's': zhconv.convert(a.get('set_zh') or a.get('set') or '', 'zh-cn'),
    }

out = {
    'ver': ver,
    'champs': champ_by_id,
    'items': item_names,
    'itemDescs': item_descs,
    'augs': augs,
    't2s': char_map,
}
out_path = ROOT / 'docs/api/names-zh-cn.json'
out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
print('wrote', out_path, out_path.stat().st_size, 'bytes')
print('champs', len(champ_by_id), 'items', len(item_names), 'augs', len(augs), 't2s', len(char_map))
# spot check
for cid in ['157','222','67','1']:
    print(cid, champ_by_id.get(cid))
