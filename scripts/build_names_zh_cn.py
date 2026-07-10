# -*- coding: utf-8 -*-
"""Rebuild names-zh-cn.json with full single-char t2s + official CN names.

Sources:
  - Champions / items: ddragon zh_CN
  - Augments: CommunityDragon cherry-augments.json (zh_cn) — same labels as
    aramkit.com/zh-CN/augments (official mainland names, not zhconv of TW)
  - Desc/set fallback: zhconv of tier-list TW strings when CD has no desc
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import zhconv
from zhconv import zhconv as zmod

ROOT = Path(".")
tier = json.loads((ROOT / "docs/api/tier-list.json").read_text(encoding="utf-8"))
ver = tier.get("ddv") or "16.13.1"

# Full single-char trad→simp map (phrase multi-char not needed for game names + UI)
full = zmod.getdict("zh-cn")
char_map = {k: v for k, v in full.items() if len(k) == 1 and k != v}
print("full single-char t2s", len(char_map))


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "arammeta-build/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


champs_cn = fetch_json(
    f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/champion.json"
)
items_cn = fetch_json(
    f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/item.json"
)

# Official Mayhem/Cherry augment names (zh_CN) — matches aramkit
cd_augs = fetch_json(
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/zh_cn/v1/cherry-augments.json"
)
cd_name_by_id = {
    str(row["id"]): (row.get("nameTRA") or "").strip()
    for row in cd_augs
    if row.get("id") is not None
}
print("cd zh_cn aug names", len(cd_name_by_id))

champ_by_id = {}
for alias, row in champs_cn["data"].items():
    cid = str(row["key"])
    # zh_CN ddragon: title is short call-name (亚索)
    champ_by_id[cid] = (row.get("title") or row.get("name") or "").strip()

item_names = {}
item_descs = {}
for iid in tier.get("itemLut") or {}:
    row = items_cn["data"].get(str(iid))
    if not row:
        continue
    item_names[str(iid)] = row.get("name") or ""
    desc = row.get("description") or ""
    desc = re.sub(r"<br\s*/?>", "\n", desc, flags=re.I)
    desc = re.sub(r"<[^>]+>", "", desc)
    item_descs[str(iid)] = desc

augs = {}
hit = 0
for aid, a in (tier.get("augs") or {}).items():
    key = str(aid)
    official = cd_name_by_id.get(key) or ""
    if official:
        hit += 1
    augs[key] = {
        "n": official
        or zhconv.convert(a.get("name_zh") or a.get("name") or "", "zh-cn"),
        "d": zhconv.convert(a.get("desc_zh") or a.get("desc") or "", "zh-cn"),
        "s": zhconv.convert(a.get("set_zh") or a.get("set") or "", "zh-cn"),
    }
print(f"augs official CD hit {hit}/{len(augs)}")

out = {
    "ver": ver,
    "champs": champ_by_id,
    "items": item_names,
    "itemDescs": item_descs,
    "augs": augs,
    "t2s": char_map,
}
out_path = ROOT / "docs/api/names-zh-cn.json"
raw = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
out_path.write_text(raw, encoding="utf-8")
print("wrote", out_path, out_path.stat().st_size)
print("check 刃", char_map.get("刃"), "箭", char_map.get("箭"))
print("3031", item_names.get("3031"), "3085", item_names.get("3085"))
for aid in ("1134", "1152", "1361", "1005", "1204", "1356", "2104", "1004", "1051", "1181"):
    print("aug", aid, augs.get(aid, {}).get("n"))
