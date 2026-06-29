"""Build docs/champion-radar.html — one strength radar per champion.

Pipeline (all from games.db, queue 2400):
  1. stream-aggregate per-champion behavioural volumes (1M+ games)
  2. derive 6 radar axes: 輸出 坦度 控場 續航 清線 參戰
  3. WITHIN-ROLE percentile  (a tank's tankiness is ranked vs other tanks),
     so a support's radar isn't crushed just because its raw damage is low
  4. reverse-infer each champ's role from play-pattern (logistic) -> flag
     champions whose behaviour doesn't match their tag
  5. per-role positive-contribution weights (axis↔winrate spearman within role)
     -> role-specific strength score, which drives radar fill colour & sort
  6. emit a self-contained review page (inline JSON + SVG radars)

Usage:
  python scripts/build_champion_radar.py            # default db + out
  python scripts/build_champion_radar.py --db data/lcu/games.db
"""
from __future__ import annotations
import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

try:
    import polars as pl
except Exception:  # pragma: no cover
    pl = None

AXES = ["輸出", "坦度", "控場", "續航", "清線", "參戰"]
ROLES = ["Marksman", "Mage", "Assassin", "Fighter", "Tank", "Support"]
ROLE_ZH = {"Marksman": "射手", "Mage": "法師", "Assassin": "刺客",
           "Fighter": "鬥士", "Tank": "坦克", "Support": "輔助"}


def aggregate(db: str, min_games: int) -> dict[int, dict]:
    con = sqlite3.connect(db)
    cur = con.cursor()
    SUM = defaultdict(lambda: defaultdict(float))
    GAMES = defaultdict(int)
    WINS = defaultdict(int)
    q = ("SELECT blue_wins, duration_sec, participants_json FROM games "
         "WHERE queue_id=2400 AND participants_json IS NOT NULL "
         "AND duration_sec IS NOT NULL AND duration_sec>300")
    n = 0
    for blue_wins, dur, pj in cur.execute(q):
        n += 1
        if n % 200000 == 0:
            print(f"  ...{n} games", flush=True)
        try:
            players = json.loads(pj)
        except Exception:
            continue
        if len(players) != 10:
            continue
        minutes = dur / 60.0
        for p in players:
            st = p.get("stats") or {}
            tid = p.get("teamId")
            cid = p.get("championId")
            if tid not in (100, 200) or cid is None:
                continue
            won = 1 if (tid == 100) == bool(blue_wins) else 0
            GAMES[cid] += 1
            WINS[cid] += won
            S = SUM[cid]
            S["min"] += minutes
            S["dmgchamp"] += st.get("total_damage_dealt_to_champions", 0) or 0
            S["dmgtotal"] += st.get("total_damage_dealt", 0) or 0
            S["taken"] += st.get("total_damage_taken", 0) or 0
            S["selfmit"] += st.get("damage_self_mitigated", 0) or 0
            S["heal"] += st.get("total_heal", 0) or 0
            S["cc"] += st.get("total_time_cc_dealt", 0) or 0
            S["kills"] += st.get("kills", 0) or 0
            S["assists"] += st.get("assists", 0) or 0
            S["gold"] += st.get("gold_earned", 0) or 0
            S["cs"] += st.get("total_minions_killed", 0) or 0
            S["phys"] += st.get("physical_damage_dealt_to_champions", 0) or 0
            S["magic"] += st.get("magic_damage_dealt_to_champions", 0) or 0
            S["true"] += st.get("true_damage_dealt_to_champions", 0) or 0
    print(f"parsed {n} games, {len(GAMES)} champions", flush=True)
    out = {}
    for cid, g in GAMES.items():
        if g < min_games:
            continue
        S = SUM[cid]
        mn = S["min"] or 1.0
        dtot_champ = S["phys"] + S["magic"] + S["true"]
        out[cid] = {
            "games": g, "win_rate": WINS[cid] / g,
            "輸出": S["dmgchamp"] / mn,
            "坦度": (S["taken"] + S["selfmit"]) / mn,
            "控場": S["cc"] / mn,
            "續航": S["heal"] / mn,
            "清線": max(S["dmgtotal"] - S["dmgchamp"], 0) / mn,
            "參戰": (S["kills"] + S["assists"]) / mn,
            # extra features for role inference
            "gold_pm": S["gold"] / mn, "cs_pm": S["cs"] / mn,
            "champdmg_frac": S["dmgchamp"] / S["dmgtotal"] if S["dmgtotal"] else 0,
            "phys_ratio": S["phys"] / dtot_champ if dtot_champ else 0,
            "magic_ratio": S["magic"] / dtot_champ if dtot_champ else 0,
        }
    return out


def pct_rank(vals: np.ndarray) -> np.ndarray:
    from scipy.stats import rankdata
    return rankdata(vals) / len(vals)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/lcu/games.db")
    ap.add_argument("--semantic", default="data/cache/champion_semantic_scores.csv")
    ap.add_argument("--out", default="docs/champion-radar.html")
    ap.add_argument("--min-games", type=int, default=1000)
    ap.add_argument("--cache", default="data/cache/champion_radar_dims.json")
    args = ap.parse_args()

    cache = Path(args.cache)
    if cache.exists():
        print(f"using cached dims {cache}")
        agg = {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    else:
        agg = aggregate(args.db, args.min_games)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(agg), encoding="utf-8")
        print(f"cached dims -> {cache}")

    sem = pl.read_csv(args.semantic)
    name_zh, name_en, tags_map = {}, {}, {}
    for r in sem.iter_rows(named=True):
        cid = int(r["champion_id"])
        name_zh[cid] = r.get("champion_name_zh") or r["champion_alias"]
        name_en[cid] = r["champion_alias"]
        tags_map[cid] = (r.get("tags") or "Fighter").split("|")

    cids = [c for c in agg if c in tags_map]
    role = {c: tags_map[c][0] for c in cids}

    # within-role percentile for the 6 radar axes
    within = {a: {} for a in AXES}
    glob = {a: {} for a in AXES}
    for a in AXES:
        allv = np.array([agg[c][a] for c in cids])
        gp = pct_rank(allv)
        for c, v in zip(cids, gp):
            glob[a][c] = float(v)
    for rl in set(role.values()):
        members = [c for c in cids if role[c] == rl]
        if not members:
            continue
        for a in AXES:
            vals = np.array([agg[c][a] for c in members])
            pr = pct_rank(vals) if len(vals) > 1 else np.array([0.5])
            for c, v in zip(members, pr):
                within[a][c] = float(v)

    # per-role positive-contribution weights (axis vs winrate spearman, clipped>=0)
    from scipy.stats import spearmanr
    role_w = {}
    for rl in set(role.values()):
        members = [c for c in cids if role[c] == rl]
        wr = np.array([agg[c]["win_rate"] for c in members])
        w = {}
        for a in AXES:
            v = np.array([within[a][c] for c in members])
            rho = spearmanr(v, wr)[0] if len(v) > 4 else 0.0
            w[a] = max(rho, 0.0) if rho == rho else 0.0
        s = sum(w.values()) or 1.0
        role_w[rl] = {a: w[a] / s for a in AXES}

    # role reverse-inference
    role_pred, role_conf = {}, {}
    try:
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_predict, StratifiedKFold
        FEATS = ["輸出", "坦度", "控場", "續航", "清線", "參戰",
                 "gold_pm", "cs_pm", "champdmg_frac", "phys_ratio", "magic_ratio"]
        X = StandardScaler().fit_transform(np.array([[agg[c][f] for f in FEATS] for c in cids]))
        classes = sorted(set(role.values()))
        yi = np.array([classes.index(role[c]) for c in cids])
        proba = cross_val_predict(LogisticRegression(max_iter=3000), X, yi,
                                  cv=StratifiedKFold(5, shuffle=True, random_state=0),
                                  method="predict_proba")
        for i, c in enumerate(cids):
            k = int(proba[i].argmax())
            role_pred[c] = classes[k]
            role_conf[c] = float(proba[i][k])
    except Exception as e:  # pragma: no cover
        print("role inference skipped:", e)
        for c in cids:
            role_pred[c] = role[c]
            role_conf[c] = 0.0

    champs = []
    for c in cids:
        rl = role[c]
        strength = sum(role_w[rl][a] * within[a][c] for a in AXES)
        pr = role_pred[c]
        mism = pr != rl and pr not in tags_map[c]
        champs.append({
            "id": c, "name": name_en[c], "zh": name_zh[c],
            "role": rl, "role_zh": ROLE_ZH.get(rl, rl),
            "wr": round(agg[c]["win_rate"] * 100, 1),
            "games": agg[c]["games"],
            "axes": [round(within[a][c], 3) for a in AXES],
            "axes_g": [round(glob[a][c], 3) for a in AXES],
            "strength": round(strength, 3),
            "pred": pr, "pred_zh": ROLE_ZH.get(pr, pr),
            "conf": round(role_conf[c] * 100),
            "mismatch": mism,
        })
    champs.sort(key=lambda d: -d["wr"])

    # role×axis sensitivity heatmap (within-role spearman, signed)
    heat = []
    for rl in ROLES:
        members = [c for c in cids if role[c] == rl]
        if len(members) < 5:
            continue
        wr = np.array([agg[c]["win_rate"] for c in members])
        rowv = []
        for a in AXES:
            v = np.array([within[a][c] for c in members])
            rho = spearmanr(v, wr)[0]
            rowv.append(round(float(rho), 2))
        heat.append({"role": ROLE_ZH.get(rl, rl), "n": len(members), "vals": rowv})

    html = HTML_TEMPLATE
    html = html.replace("__AXES__", json.dumps(AXES, ensure_ascii=False))
    html = html.replace("__CHAMPS__", json.dumps(champs, ensure_ascii=False))
    html = html.replace("__HEAT__", json.dumps(heat, ensure_ascii=False))
    html = html.replace("__ROLEW__", json.dumps(
        {ROLE_ZH.get(k, k): {a: round(v, 2) for a, v in w.items()} for k, w in role_w.items()},
        ensure_ascii=False))
    html = html.replace("__N__", str(len(champs)))
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"wrote {args.out}  ({len(champs)} champions)")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>英雄能力雷達 · ARAM Mayhem</title>
<style>
:root{color-scheme:light;--bg:#f6f3ea;--ink:#20231f;--muted:#667064;--line:#d7d0c2;
--panel:#fffdf7;--panel2:#ece5d6;--accent:#0f766e;--win:#1d9e75;--lose:#d85a30;
--shadow:0 14px 40px rgba(34,31,24,.10);}
*{box-sizing:border-box;}
body{margin:0;background:linear-gradient(180deg,rgba(15,118,110,.07),transparent 22rem),var(--bg);
color:var(--ink);font-family:ui-sans-serif,"Segoe UI","Noto Sans TC","Microsoft JhengHei",sans-serif;}
header{position:sticky;top:0;z-index:20;border-bottom:1px solid var(--line);
background:rgba(246,243,234,.93);backdrop-filter:blur(14px);}
.bar{max-width:1500px;margin:0 auto;padding:1rem clamp(1rem,2vw,2rem);}
h1{margin:0;font-size:clamp(1.2rem,2vw,1.8rem);font-weight:800;}
.sub{margin-top:.3rem;color:var(--muted);font-size:.85rem;line-height:1.5;max-width:70ch;}
.controls{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.8rem;align-items:center;}
input,select,button{font:inherit;color:var(--ink);background:var(--panel);
border:1px solid var(--line);border-radius:8px;padding:.4rem .6rem;}
button{cursor:pointer;}
button.on{background:var(--accent);color:#fff;border-color:var(--accent);}
.wrap{max-width:1500px;margin:0 auto;padding:1.2rem clamp(1rem,2vw,2rem) 4rem;}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:1rem 1.2rem;margin-bottom:1.4rem;box-shadow:var(--shadow);}
.panel h2{margin:.2rem 0 .8rem;font-size:1rem;}
.heat{border-collapse:separate;border-spacing:3px;width:100%;max-width:640px;font-size:.82rem;}
.heat th{color:var(--muted);font-weight:600;padding:3px;}
.heat td{text-align:center;padding:7px 0;border-radius:6px;color:#fff;font-variant-numeric:tabular-nums;}
.heat .rl{color:var(--ink);text-align:left;font-weight:600;background:none!important;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.8rem;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:.7rem;
text-align:center;cursor:pointer;transition:transform .08s,box-shadow .08s;position:relative;}
.card:hover{transform:translateY(-2px);box-shadow:var(--shadow);}
.card .nm{font-weight:700;font-size:.92rem;margin-top:.2rem;}
.card .rl{font-size:.72rem;color:var(--muted);}
.card .wr{font-size:.86rem;font-weight:700;font-variant-numeric:tabular-nums;}
.flag{position:absolute;top:.4rem;right:.4rem;font-size:.66rem;background:var(--lose);
color:#fff;border-radius:6px;padding:1px 5px;}
.legend{color:var(--muted);font-size:.78rem;margin-top:.6rem;line-height:1.6;}
.modal{position:fixed;inset:0;background:rgba(20,18,14,.5);display:none;z-index:50;
align-items:center;justify-content:center;padding:1rem;}
.modal.show{display:flex;}
.sheet{background:var(--panel);border-radius:16px;padding:1.4rem;max-width:430px;width:100%;
box-shadow:var(--shadow);}
.sheet h3{margin:0;font-size:1.3rem;}
.row{display:flex;justify-content:space-between;font-size:.86rem;padding:.25rem 0;border-bottom:1px dashed var(--line);}
.tag{display:inline-block;font-size:.74rem;background:var(--panel2);border-radius:6px;padding:2px 8px;margin-right:.3rem;}
</style>
</head>
<body>
<header><div class="bar">
<h1>英雄能力雷達 · ARAM Mayhem</h1>
<div class="sub">每個英雄一張雷達，六軸＝該英雄在<b>同職業內</b>的百分位（坦度跟坦克比、輸出跟法師比）。
填色＝勝率，雷達越大代表該英雄在同類中越全面。資料來自百萬場 Mayhem。
★標記＝打法與標註職業不符。</div>
<div class="controls">
<input id="q" placeholder="搜尋英雄…" style="min-width:150px;"/>
<span id="roleBtns"></span>
<select id="sort"><option value="wr">依勝率</option><option value="strength">依強度分</option><option value="name">依名稱</option></select>
<label style="font-size:.8rem;color:var(--muted);"><input type="checkbox" id="onlyMis"/> 只看定位不符</label>
</div>
</div></header>
<div class="wrap">
<div class="panel">
<h2>職業 × 軸 的勝率敏感度（每職業該看哪些軸）</h2>
<table class="heat" id="heat"></table>
<div class="legend">藍＝該軸越高、該職業勝率越高；紅＝越高反而越弱。這就是各職業雷達強度分的加權依據。</div>
</div>
<div class="panel" style="padding:.8rem 1.2rem;"><div id="count" style="color:var(--muted);font-size:.85rem;"></div></div>
<div class="grid" id="grid"></div>
</div>
<div class="modal" id="modal"><div class="sheet" id="sheet"></div></div>
<script>
const AXES=__AXES__, CH=__CHAMPS__, HEAT=__HEAT__, ROLEW=__ROLEW__;
const N=__N__;
function radar(vals,fill,size,labels){
 const cx=size/2,cy=size/2,R=size/2-(labels?26:6),n=vals.length;
 let rings='';
 for(const f of [0.33,0.66,1]){let p='';for(let i=0;i<n;i++){const a=-Math.PI/2+i*2*Math.PI/n;
  p+=(i?'L':'M')+(cx+R*f*Math.cos(a)).toFixed(1)+' '+(cy+R*f*Math.sin(a)).toFixed(1)+' ';}
  rings+='<path d="'+p+'Z" fill="none" stroke="#d7d0c2" stroke-width="1"/>';}
 let axesL='';for(let i=0;i<n;i++){const a=-Math.PI/2+i*2*Math.PI/n;
  axesL+='<line x1="'+cx+'" y1="'+cy+'" x2="'+(cx+R*Math.cos(a)).toFixed(1)+'" y2="'+(cy+R*Math.sin(a)).toFixed(1)+'" stroke="#d7d0c2" stroke-width="1"/>';
  if(labels){const lx=cx+(R+15)*Math.cos(a),ly=cy+(R+15)*Math.sin(a);
   axesL+='<text x="'+lx.toFixed(1)+'" y="'+ly.toFixed(1)+'" font-size="11" fill="#667064" text-anchor="middle" dominant-baseline="middle">'+labels[i]+'</text>';}}
 let p='';for(let i=0;i<n;i++){const a=-Math.PI/2+i*2*Math.PI/n,r=R*vals[i];
  p+=(i?'L':'M')+(cx+r*Math.cos(a)).toFixed(1)+' '+(cy+r*Math.sin(a)).toFixed(1)+' ';}
 return '<svg viewBox="0 0 '+size+' '+size+'" width="'+size+'" height="'+size+'">'+rings+axesL+
  '<path d="'+p+'Z" fill="'+fill+'" fill-opacity="0.22" stroke="'+fill+'" stroke-width="2"/></svg>';
}
function wrColor(wr){const t=Math.max(-1,Math.min(1,(wr-50)/7));
 const g=[136,135,128];const c=t>=0?[29,158,117]:[216,90,48];const a=Math.abs(t);
 return 'rgb('+Math.round(g[0]+(c[0]-g[0])*a)+','+Math.round(g[1]+(c[1]-g[1])*a)+','+Math.round(g[2]+(c[2]-g[2])*a)+')';}
// heatmap
(function(){let h='<tr><th></th>';for(const a of AXES)h+='<th>'+a+'</th></tr>';
 for(const r of HEAT){h+='<tr><td class="rl">'+r.role+'<span style="color:#a9a596;font-weight:400"> '+r.n+'</span></td>';
  const star=r.vals.map(Math.abs).indexOf(Math.max(...r.vals.map(Math.abs)));
  r.vals.forEach((v,i)=>{const al=Math.min(1,Math.abs(v)/0.6)*0.82;
   const bg=v>=0?'rgba(29,110,134,'+al+')':'rgba(216,90,48,'+al+')';
   h+='<td style="background:'+bg+';'+(i===star?'outline:2px solid #20231f;':'')+'">'+(v>=0?'+':'')+v.toFixed(2)+'</td>';});
  h+='</tr>';}
 document.getElementById('heat').innerHTML=h;})();
// role filter buttons
const roles=[...new Set(CH.map(c=>c.role_zh))];let activeRole='';
let rb='<button class="on" data-r="">全部</button>';
for(const r of roles)rb+='<button data-r="'+r+'">'+r+'</button>';
document.getElementById('roleBtns').innerHTML=rb;
document.getElementById('roleBtns').onclick=e=>{if(e.target.dataset.r===undefined)return;
 activeRole=e.target.dataset.r;[...e.currentTarget.children].forEach(b=>b.classList.toggle('on',b.dataset.r===activeRole));render();};
function render(){
 const q=document.getElementById('q').value.trim().toLowerCase();
 const sort=document.getElementById('sort').value;
 const onlyMis=document.getElementById('onlyMis').checked;
 let list=CH.filter(c=>(!activeRole||c.role_zh===activeRole)&&(!onlyMis||c.mismatch)&&
  (!q||c.name.toLowerCase().includes(q)||c.zh.includes(q)));
 if(sort==='name')list.sort((a,b)=>a.name<b.name?-1:1);
 else list.sort((a,b)=>b[sort]-a[sort]);
 document.getElementById('count').textContent='顯示 '+list.length+' / '+N+' 英雄';
 let h='';for(const c of list){const col=wrColor(c.wr);
  h+='<div class="card" data-id="'+c.id+'">'+(c.mismatch?'<span class="flag">★'+c.pred_zh+'</span>':'')+
   radar(c.axes,col,118)+'<div class="nm">'+c.zh+'</div><div class="rl">'+c.role_zh+'</div>'+
   '<div class="wr" style="color:'+col+'">'+c.wr+'%</div></div>';}
 document.getElementById('grid').innerHTML=h;
}
['q','sort','onlyMis'].forEach(id=>document.getElementById(id).addEventListener('input',render));
document.getElementById('grid').onclick=e=>{const card=e.target.closest('.card');if(!card)return;
 const c=CH.find(x=>x.id==card.dataset.id);const col=wrColor(c.wr);
 const w=ROLEW[c.role_zh]||{};
 let rows='';AXES.forEach((a,i)=>{rows+='<div class="row"><span>'+a+' <span style="color:#a9a596">(權重 '+((w[a]||0)*100).toFixed(0)+'%)</span></span><span>'+Math.round(c.axes[i]*100)+' 百分位</span></div>';});
 document.getElementById('sheet').innerHTML='<h3>'+c.zh+' <span style="font-size:.8rem;color:#667064">'+c.name+'</span></h3>'+
  '<div style="margin:.4rem 0"><span class="tag">'+c.role_zh+'</span>'+(c.mismatch?'<span class="tag" style="background:#f5d9cc">打法像 '+c.pred_zh+' '+c.conf+'%</span>':'')+
  '<span class="tag">勝率 '+c.wr+'%</span><span class="tag">'+c.games.toLocaleString()+' 場</span></div>'+
  '<div style="text-align:center">'+radar(c.axes,col,260,AXES)+'</div>'+rows+
  '<div class="legend">六軸＝職業內百分位。權重＝該軸對此職業勝率的正貢獻，強度分 '+Math.round(c.strength*100)+'。</div>';
 document.getElementById('modal').classList.add('show');};
document.getElementById('modal').onclick=e=>{if(e.target.id==='modal')e.currentTarget.classList.remove('show');};
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
