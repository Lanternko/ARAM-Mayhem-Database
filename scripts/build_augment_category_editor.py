"""Generate a self-contained HTML editor for fixing augment categories.

The site's augment chips (AP / AD / 坦度 / 金錢 / 機制 / CD / 新 / 暴擊 / 增傷) are
auto-derived by keyword matching in ``augment_filter_categories``
(scripts/build_tier_list.py).  That heuristic mislabels some augments.  This
tool reads the current ``cats`` from ``docs/api/tier-list.json`` and renders a
checkbox grid so the categories can be hand-corrected, then exports an override
map ``{aid: [cats]}`` that can be fed back into the build.

Run:
    python scripts/build_augment_category_editor.py

Output: augment-category-editor.html  (open by double-clicking; icons load from
the GitHub Pages site so no local server is needed).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Same directory as build_tier_list.py — reuse its CommunityDragon description
# resolver so the editor can backfill effect text for augments whose desc is
# blank in a stale tier-list.json (otherwise they can't be hand-classified).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_tier_list as _btl  # noqa: E402

SITE = "https://arammeta.com/"

# 輔助 (Support) chip.  Defined canonically in build_tier_list.py; mirrored here
# so the editor shows the column even when reading a tier-list.json generated
# before the category existed (avoids needing a full, heavy site rebuild first).
# Seeded from Riot's own Ally (0) + Support (5) displayTags.
SUPPORT_CAT = "support"
SUPPORT_LABEL = {"zh": "輔助", "en": "Support"}
SUPPORT_DISPLAY_TAGS = (0, 5)

# Same curated overrides the site build reads (build_tier_list.py).  Applying
# them here makes the editor open with your corrections as the baseline, so a
# fresh load shows nothing as "modified" until you change something new.
OVERRIDES_PATH = Path(__file__).resolve().parent / "augment_category_overrides.json"


def load_overrides() -> dict[int, list[str]]:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[int, list[str]] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            try:
                out[int(key)] = [c for c in val if isinstance(c, str)]
            except (TypeError, ValueError):
                continue
    return out


def icon_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return SITE + path.lstrip("/")


def build_data(tier_list_path: Path, cache_dir: Path = Path("data/cache")) -> dict:
    payload = json.loads(tier_list_path.read_text(encoding="utf-8"))
    augs = payload.get("augs", {})
    # Backfill descriptions from the local CommunityDragon cache so newly-added
    # augments (blank in a stale payload) still show their effect text.
    desc_zh: dict[int, str] = {}
    desc_en: dict[int, str] = {}
    try:
        desc_zh = _btl.load_augment_descriptions(
            cache_dir, locale="zh_tw", cache_name="lol_stringtable_zh_tw.json"
        )
        desc_en = _btl.load_augment_descriptions(
            cache_dir, locale="en_us", cache_name="lol_stringtable_en_us.json"
        )
    except Exception:
        pass
    cats_meta = payload.get("augCategories", {})
    order = list(cats_meta.get("order") or [])
    labels = dict(cats_meta.get("labels") or {})
    if SUPPORT_CAT not in order:
        pos = order.index("tank") + 1 if "tank" in order else len(order)
        order.insert(pos, SUPPORT_CAT)
    labels.setdefault(SUPPORT_CAT, SUPPORT_LABEL)

    overrides = load_overrides()
    augments = []
    for aid, a in augs.items():
        base = list(a.get("cats") or [])
        display_tags = a.get("displayTags") or []
        key = int(aid)
        ov = overrides.get(key)
        if ov is not None:
            cats = [c for c in ov if c != "new"]  # curated correction wins
        else:
            cats = [c for c in base if c != "new"]
            if SUPPORT_CAT not in cats and any(
                t in SUPPORT_DISPLAY_TAGS for t in display_tags
            ):
                cats.append(SUPPORT_CAT)
        # `new` is dynamic: show it if the current build OR the saved override
        # marks it new, so it survives a stale tier-list.json snapshot.
        if ("new" in base) or (ov is not None and "new" in ov):
            cats.append("new")
        cats = [c for c in order if c in cats]  # canonical order
        augments.append(
            {
                "id": int(aid),
                "name": a.get("name_zh") or a.get("name") or f"#{aid}",
                "name_en": a.get("name_en") or "",
                "icon": icon_url(a.get("icon", "")),
                "rarity": a.get("rarity", ""),
                "desc": (
                    a.get("desc_zh") or a.get("desc")
                    or desc_zh.get(int(aid)) or desc_en.get(int(aid)) or ""
                ),
                "cats": cats,
            }
        )
    augments.sort(key=lambda x: x["name"] or "")
    return {
        "site": SITE,
        "categories": {"order": order, "labels": labels},
        "augments": augments,
    }


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>增益分類校正 · Augment Category Editor</title>
<style>
  :root{
    --bg:#0f141b; --panel:#161d27; --panel2:#1c2531; --line:#26313f;
    --text:#e6edf5; --muted:#8b97a7; --accent:#d9a441; --accent2:#5aa9e6;
    --ok:#46c98b; --mod:#e0b552; --chip:#222c39; --chip-on:#2f4a63;
    --silver:#9aa4b2; --gold:#e0b552; --prism:#7bd6e0;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:var(--bg); color:var(--text);
    font-family:"Segoe UI","Microsoft JhengHei",system-ui,sans-serif;
    font-size:14px; display:flex; flex-direction:column; height:100vh;
  }
  a{color:var(--accent2)}
  .topbar{
    display:flex; align-items:center; gap:16px; padding:10px 16px;
    background:linear-gradient(180deg,#141b24,#10161e);
    border-bottom:1px solid var(--line); flex:0 0 auto;
  }
  .topbar h1{font-size:16px; margin:0; font-weight:650; letter-spacing:.3px}
  .topbar h1 small{color:var(--muted); font-weight:400; margin-left:6px; font-size:12px}
  .counts{color:var(--muted); font-size:12.5px}
  .counts b{color:var(--text)} .counts .mod{color:var(--mod)} .counts .un{color:#e07a7a}
  .spacer{flex:1}
  .btn{
    background:var(--chip); color:var(--text); border:1px solid var(--line);
    border-radius:7px; padding:7px 12px; font-size:12.5px; cursor:pointer;
    transition:.12s; white-space:nowrap;
  }
  .btn:hover{background:var(--chip-on); border-color:#3a5066}
  .btn.primary{background:var(--accent); color:#1a1206; border-color:var(--accent); font-weight:600}
  .btn.primary:hover{filter:brightness(1.08)}
  .btn.ghost{background:transparent}
  .toolbar{
    display:flex; align-items:center; gap:8px; padding:9px 16px; flex-wrap:wrap;
    background:var(--panel); border-bottom:1px solid var(--line); flex:0 0 auto;
  }
  .search{
    background:var(--bg); border:1px solid var(--line); border-radius:7px;
    padding:7px 11px; color:var(--text); min-width:220px; font-size:13px;
  }
  .search:focus{outline:none; border-color:var(--accent2)}
  .fchip{
    background:var(--chip); border:1px solid var(--line); color:var(--muted);
    border-radius:999px; padding:5px 12px; font-size:12px; cursor:pointer; transition:.12s;
  }
  .fchip:hover{color:var(--text)}
  .fchip.on{background:var(--chip-on); color:var(--text); border-color:#3a5066}
  .fchip.on.mod{background:#4a3a1a; border-color:#7a611f; color:#f0cd77}
  .fchip.on.un{background:#4a2020; border-color:#7a2f2f; color:#f0a0a0}
  .tablewrap{flex:1 1 auto; overflow:auto; position:relative}
  .grid{min-width:max-content}
  .row,.head{display:grid; grid-template-columns:var(--cols); align-items:stretch}
  .head{
    position:sticky; top:0; z-index:5;
    background:var(--panel2); border-bottom:1px solid var(--line);
  }
  .head .gh{
    padding:8px 4px; text-align:center; font-size:11.5px; color:var(--muted);
    border-left:1px solid var(--line); display:flex; flex-direction:column;
    justify-content:center; gap:1px; user-select:none;
  }
  .head .gh b{color:var(--text); font-size:13px; font-weight:600}
  .head .gh.aughdr, .row .aug{
    position:sticky; left:0; z-index:1;
  }
  .head .gh.aughdr{
    z-index:6; text-align:left; padding-left:14px; align-items:flex-start;
    border-left:none; background:var(--panel2);
  }
  .head .gh small{font-size:10px; opacity:.7}
  .row{border-bottom:1px solid #1b232e}
  .row:hover{background:#172131}
  .row.modified{background:#1d1e16}
  .row.modified:hover{background:#23241a}
  .row.hidden{display:none}
  .aug{
    display:flex; gap:10px; align-items:center; padding:8px 10px 8px 8px;
    background:var(--bg); border-left:3px solid transparent; min-width:0;
  }
  .row:hover .aug{background:#161f2c}
  .row.modified .aug{background:#1c1d14}
  .aug[data-rarity="kSilver"]{border-left-color:var(--silver)}
  .aug[data-rarity="kGold"]{border-left-color:var(--gold)}
  .aug[data-rarity="kPrismatic"]{border-left-color:var(--prism)}
  .aug img{
    width:34px; height:34px; border-radius:6px; flex:0 0 auto;
    background:#0a0e13; object-fit:cover;
  }
  .aug .meta{min-width:0}
  .aug .nm{font-weight:600; font-size:13.5px; line-height:1.2;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .aug .en{color:var(--muted); font-size:11px; margin-left:0}
  .aug .desc{
    color:#9fb0c2; font-size:11px; line-height:1.3; margin-top:2px;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
    overflow:hidden; max-width:520px;
  }
  .modtag{
    display:none; font-size:9.5px; color:#1a1206; background:var(--mod);
    border-radius:4px; padding:0 5px; margin-left:6px; vertical-align:middle; font-weight:700;
  }
  .row.modified .modtag{display:inline-block}
  .cell{
    display:flex; align-items:center; justify-content:center;
    border-left:1px solid #1b232e; cursor:pointer;
  }
  .cell input{width:17px; height:17px; cursor:pointer; accent-color:var(--accent)}
  .cell:hover{background:#1f2c3b}
  .empty{padding:40px; text-align:center; color:var(--muted)}
  .statusbar{
    flex:0 0 auto; padding:6px 16px; font-size:11.5px; color:var(--muted);
    border-top:1px solid var(--line); background:var(--panel);
    display:flex; align-items:center; gap:14px;
  }
  .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:5px}
  .hidden-input{display:none}
  kbd{background:#222c39;border:1px solid var(--line);border-radius:4px;padding:1px 5px;font-size:10.5px}
</style>
</head>
<body>
  <div class="topbar">
    <h1>增益分類校正 <small>Augment Category Editor</small></h1>
    <div class="counts" id="counts"></div>
    <div class="spacer"></div>
    <button class="btn primary" id="exportDiff">匯出修正</button>
    <button class="btn ghost" id="exportAll">匯出全部</button>
    <button class="btn ghost" id="importBtn">匯入</button>
    <button class="btn ghost" id="loadBtn">載入 tier-list.json</button>
    <button class="btn ghost" id="resetBtn">全部還原</button>
    <input type="file" id="importFile" class="hidden-input" accept="application/json,.json">
    <input type="file" id="loadFile" class="hidden-input" accept="application/json,.json">
  </div>
  <div class="toolbar">
    <input class="search" id="search" placeholder="搜尋增益名稱 (中 / EN)…">
    <span id="filters"></span>
  </div>
  <div class="tablewrap">
    <div class="grid" id="grid"></div>
    <div class="empty" id="empty" style="display:none">沒有符合條件的增益</div>
  </div>
  <div class="statusbar">
    <span id="saveState"><span class="dot"></span>變更會自動存到瀏覽器</span>
    <span>勾選 = 該增益屬於此分類。修改過的列會標記 <span style="color:var(--mod)">●</span> 並可用「已修改」篩選。</span>
    <span class="spacer" style="flex:1"></span>
    <span>匯出修正 → <kbd>augment_category_overrides.json</kbd></span>
  </div>

<script>
const DATA = __DATA__;
const SITE = DATA.site;
const STORE_KEY = "augCatEditor.v1";

function iconUrl(p){
  if(!p) return "";
  if(p.indexOf("http")===0) return p;
  return SITE + p.replace(/^\/+/,"");
}
function esc(s){
  return String(s==null?"":s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}
function sameSet(a,b){
  if(a.length!==b.length) return false;
  const sa=[...a].sort(), sb=[...b].sort();
  return sa.every((v,i)=>v===sb[i]);
}

let CATS = DATA.categories;            // {order, labels}
let AUGS = [];                         // working augments with ._orig
let filter = "all";                    // all | modified | uncat | cat:<key>
let query = "";

function initAugs(list){
  AUGS = list.map(a => ({...a, cats:[...a.cats], _orig:[...a.cats]}));
  applySaved();
}
function applySaved(){
  let saved={};
  try{ saved = JSON.parse(localStorage.getItem(STORE_KEY)||"{}"); }catch(e){}
  for(const a of AUGS){
    if(saved[a.id]) a.cats = [...saved[a.id]];
  }
}
function isMod(a){ return !sameSet(a.cats, a._orig); }
function saveDiffs(){
  const diff={};
  for(const a of AUGS){ if(isMod(a)) diff[a.id]=[...a.cats].sort(); }
  localStorage.setItem(STORE_KEY, JSON.stringify(diff));
  const n=Object.keys(diff).length;
  document.getElementById("saveState").innerHTML =
    '<span class="dot"></span>已存 '+n+' 筆修正到瀏覽器';
}

function labelOf(key){
  const l = CATS.labels[key]||{};
  return {zh:l.zh||key, en:l.en||key};
}

function buildHead(){
  const order=CATS.order;
  document.getElementById("grid").style.setProperty(
    "--cols", "minmax(260px,1.4fr) repeat("+order.length+", 52px)");
  let h='<div class="head"><div class="gh aughdr"><b>增益 Augment</b><small>'+AUGS.length+' 個 · 由 Riot 關鍵字自動分類，請手動校正</small></div>';
  for(const k of order){
    const L=labelOf(k);
    h+='<div class="gh" title="'+esc(L.en)+'"><b>'+esc(L.zh)+'</b><small>'+esc(L.en)+'</small></div>';
  }
  h+='</div>';
  return h;
}

function matchFilter(a){
  if(query){
    const q=query.toLowerCase();
    if((a.name||"").toLowerCase().indexOf(q)<0 &&
       (a.name_en||"").toLowerCase().indexOf(q)<0) return false;
  }
  if(filter==="all") return true;
  if(filter==="modified") return isMod(a);
  if(filter==="uncat") return a.cats.length===0;
  if(filter.indexOf("cat:")===0) return a.cats.indexOf(filter.slice(4))>=0;
  return true;
}

function rowHtml(a){
  const order=CATS.order;
  let cells="";
  for(const k of order){
    const on=a.cats.indexOf(k)>=0;
    cells+='<label class="cell"><input type="checkbox" data-id="'+a.id+
           '" data-cat="'+k+'"'+(on?" checked":"")+'></label>';
  }
  return '<div class="row'+(isMod(a)?" modified":"")+'" data-id="'+a.id+'">'+
    '<div class="aug" data-rarity="'+esc(a.rarity)+'">'+
      (a.icon?'<img loading="lazy" src="'+esc(a.icon)+'" onerror="this.style.visibility=\'hidden\'">':'<img>')+
      '<div class="meta">'+
        '<div class="nm">'+esc(a.name)+
          ' <span class="en">'+esc(a.name_en)+'</span>'+
          '<span class="modtag">已改</span></div>'+
        (a.desc?'<div class="desc" title="'+esc(a.desc)+'">'+esc(a.desc)+'</div>':'')+
      '</div>'+
    '</div>'+ cells +'</div>';
}

function render(){
  const grid=document.getElementById("grid");
  const rows=AUGS.filter(matchFilter);
  grid.innerHTML = buildHead() + rows.map(rowHtml).join("");
  document.getElementById("empty").style.display = rows.length?"none":"block";
  renderCounts();
  renderFilters();
}
function renderCounts(){
  const total=AUGS.length;
  const mod=AUGS.filter(isMod).length;
  const un=AUGS.filter(a=>a.cats.length===0).length;
  document.getElementById("counts").innerHTML =
    '共 <b>'+total+'</b> ・ 已修改 <span class="mod"><b>'+mod+'</b></span> ・ 未分類 <span class="un"><b>'+un+'</b></span>';
}
function renderFilters(){
  const order=CATS.order;
  let h='';
  h+=chip("all","全部");
  h+=chip("modified","已修改","mod");
  h+=chip("uncat","未分類","un");
  for(const k of order){ h+=chip("cat:"+k, labelOf(k).zh); }
  document.getElementById("filters").innerHTML=h;
}
function chip(key,label,extra){
  const on = filter===key;
  return '<span class="fchip'+(on?" on":"")+(extra&&on?" "+extra:"")+
    '" data-f="'+esc(key)+'">'+esc(label)+'</span>';
}

/* ---- events (delegated) ---- */
document.getElementById("grid").addEventListener("change", e=>{
  const cb=e.target.closest('input[type=checkbox]'); if(!cb) return;
  const id=parseInt(cb.dataset.id,10), cat=cb.dataset.cat;
  const a=AUGS.find(x=>x.id===id); if(!a) return;
  const i=a.cats.indexOf(cat);
  if(cb.checked && i<0) a.cats.push(cat);
  else if(!cb.checked && i>=0) a.cats.splice(i,1);
  // reorder to canonical order
  a.cats = CATS.order.filter(k=>a.cats.indexOf(k)>=0);
  const row=cb.closest(".row");
  if(row) row.classList.toggle("modified", isMod(a));
  saveDiffs(); renderCounts();
  if(filter==="modified" || filter==="uncat" || filter.indexOf("cat:")===0){
    // membership may have changed -> refresh list
    render();
  }
});
document.getElementById("filters").addEventListener("click", e=>{
  const c=e.target.closest(".fchip"); if(!c) return;
  filter=c.dataset.f; render();
});
document.getElementById("search").addEventListener("input", e=>{
  query=e.target.value.trim(); render();
});

/* ---- export / import ---- */
function download(name, obj){
  const blob=new Blob([JSON.stringify(obj,null,2)], {type:"application/json"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url; a.download=name; a.click();
  setTimeout(()=>URL.revokeObjectURL(url), 1000);
}
document.getElementById("exportDiff").addEventListener("click", ()=>{
  const out={};
  for(const a of AUGS){ if(isMod(a)) out[a.id]=[...a.cats]; }
  if(!Object.keys(out).length){ alert("目前沒有任何修改。"); return; }
  download("augment_category_overrides.json", out);
});
document.getElementById("exportAll").addEventListener("click", ()=>{
  const out={};
  for(const a of AUGS){ out[a.id]=[...a.cats]; }
  download("augment_categories_full.json", out);
});
document.getElementById("importBtn").addEventListener("click",
  ()=>document.getElementById("importFile").click());
document.getElementById("importFile").addEventListener("change", e=>{
  const f=e.target.files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=()=>{
    try{
      const obj=JSON.parse(r.result);
      let n=0;
      for(const a of AUGS){
        let v=obj[a.id]!=null?obj[a.id]:obj[String(a.id)];
        if(v==null) continue;
        if(v && !Array.isArray(v) && Array.isArray(v.cats)) v=v.cats;
        if(Array.isArray(v)){ a.cats=CATS.order.filter(k=>v.indexOf(k)>=0); n++; }
      }
      saveDiffs(); render();
      alert("已匯入 "+n+" 筆分類。");
    }catch(err){ alert("匯入失敗：" + err.message); }
  };
  r.readAsText(f); e.target.value="";
});
document.getElementById("loadBtn").addEventListener("click",
  ()=>document.getElementById("loadFile").click());
document.getElementById("loadFile").addEventListener("change", e=>{
  const f=e.target.files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=()=>{
    try{
      const d=JSON.parse(r.result);
      const augs=d.augs||{}; const list=[];
      for(const [aid,a] of Object.entries(augs)){
        list.push({
          id:parseInt(aid,10),
          name:a.name_zh||a.name||("#"+aid),
          name_en:a.name_en||"",
          icon:iconUrl(a.icon||""),
          rarity:a.rarity||"",
          desc:a.desc_zh||a.desc||"",
          cats:Array.isArray(a.cats)?[...a.cats]:[],
        });
      }
      list.sort((x,y)=>(x.name||"").localeCompare(y.name||"","zh-Hant"));
      if(d.augCategories && d.augCategories.order) CATS=d.augCategories;
      initAugs(list); render();
      alert("已載入 "+list.length+" 個增益（你之前的修正會自動套用）。");
    }catch(err){ alert("載入失敗：" + err.message); }
  };
  r.readAsText(f); e.target.value="";
});
document.getElementById("resetBtn").addEventListener("click", ()=>{
  if(!confirm("確定要把所有分類還原成自動分類的原始值？（會清除你的修正）")) return;
  for(const a of AUGS) a.cats=[...a._orig];
  localStorage.removeItem(STORE_KEY);
  document.getElementById("saveState").innerHTML='<span class="dot"></span>已還原';
  render();
});

/* ---- boot ---- */
initAugs(DATA.augments);
render();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tier-list",
        type=Path,
        default=Path("docs/api/tier-list.json"),
        help="Source tier-list.json with augs + augCategories.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("augment-category-editor.html"),
        help="Output HTML path.",
    )
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("data/cache"),
        help="CommunityDragon cache dir used to backfill blank descriptions.",
    )
    args = ap.parse_args()

    data = build_data(args.tier_list, args.cache_dir)
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", data_json)
    args.out.write_text(html, encoding="utf-8")
    n = len(data["augments"])
    uncat = sum(1 for a in data["augments"] if not a["cats"])
    print(f"Wrote {args.out}  ({n} augments, {uncat} uncategorized)")


if __name__ == "__main__":
    main()
