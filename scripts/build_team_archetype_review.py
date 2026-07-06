"""Render the Q2 team-archetype clustering result (ablation_team_archetype_clusters.py)
into a static dark-theme review HTML, matching the house bucket-review style.

Reads the cross-patch JSON as the headline split and the within-patch JSON as a
robustness section.  Re-runnable: regenerate the JSONs, re-run this.

    python scripts/build_team_archetype_review.py
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import click

# zh labels for the 9 capability dims (COMP_STAT_KEYS order is preserved in JSON).
DIM_ZH = {
    "front": "前排", "engage": "開戰", "poke": "消耗", "magic": "魔傷", "phys": "物傷",
    "sustain": "續航", "cc": "控場", "wave": "清線", "damage": "傷害",
}
ALL_HAND = {
    "dive": "Dive/衝臉", "poke": "Poke/消耗", "adc": "AD carry/物理後排",
    "mage": "Mage core/法師核心", "sustain": "Sustain/續航", "frontback": "Front-to-back/前後排",
}

CSS = """
:root{color-scheme:dark;--bg:#0e1116;--panel:#161a22;--panel-2:#1f2530;--line:#30363d;
--line-soft:#252b35;--text:#e6e8eb;--muted:#9aa0a6;--dim:#6f7784;--accent:#8bdbff;
--good:#9ee66f;--warn:#f6c760;--bad:#ff7b72;--focus:#d8b8ff;--radius:8px;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font-size:13px;line-height:1.5;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;}
header{position:sticky;top:0;z-index:30;background:rgba(14,17,22,.94);border-bottom:1px solid var(--line);backdrop-filter:blur(14px);padding:14px 22px 12px;}
h1{margin:0;font-size:19px;font-weight:700;}
.meta{margin-top:3px;color:var(--muted);font-size:12px;}
main{max-width:1180px;margin:0 auto;padding:22px;}
section{margin-bottom:34px;}
h2{font-size:15px;color:var(--accent);border-bottom:1px solid var(--line-soft);padding-bottom:6px;margin:0 0 14px;}
.verdict{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-bottom:18px;}
.vcard{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:13px 15px;}
.vcard .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px;}
.vcard .v{font-size:20px;font-weight:700;margin:3px 0;}
.vcard .d{color:var(--dim);font-size:12px;}
.sweep{display:flex;gap:3px;align-items:flex-end;height:64px;margin:6px 0 2px;}
.sweep .b{flex:1;background:var(--panel-2);border-radius:3px 3px 0 0;position:relative;}
.sweep .b.peak{background:var(--accent);}
.sweep .b span{position:absolute;bottom:-17px;left:0;right:0;text-align:center;font-size:10px;color:var(--dim);}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:13px;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px 15px;}
.card.good{border-left:3px solid var(--good);}
.card.bad{border-left:3px solid var(--bad);}
.card.mid{border-left:3px solid var(--line);}
.chead{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:9px;}
.chead .arch{font-weight:700;font-size:14px;}
.chead .sz{color:var(--dim);font-size:11px;white-space:nowrap;}
.stat{display:flex;gap:16px;margin:9px 0 11px;font-size:12px;}
.stat b{font-size:15px;}
.stat .lbl{color:var(--muted);display:block;font-size:11px;}
.pos{color:var(--good);}.neg{color:var(--bad);}.dim{color:var(--dim);}
.bars{display:grid;grid-template-columns:auto 1fr auto;gap:3px 8px;align-items:center;font-size:11px;}
.bars .dl{color:var(--muted);text-align:right;}
.bars .track{height:8px;background:var(--panel-2);border-radius:4px;overflow:hidden;}
.bars .fill{height:100%;background:var(--dim);}
.bars .fill.hi{background:var(--accent);}
.bars .pv{color:var(--dim);font-variant-numeric:tabular-nums;width:30px;text-align:right;}
.reps{margin-top:11px;display:flex;flex-wrap:wrap;gap:5px;}
.chip{background:var(--panel-2);border:1px solid var(--line-soft);border-radius:12px;padding:2px 9px;font-size:11px;color:var(--text);}
.chip .lf{color:var(--dim);}
.note{background:var(--panel);border:1px solid var(--line-soft);border-radius:var(--radius);padding:12px 15px;color:var(--muted);font-size:12px;}
.note b{color:var(--text);}
.miss{color:var(--warn);}
"""


def bar_rows(centroid: dict) -> str:
    rows = []
    ordered = sorted(centroid.items(), key=lambda kv: kv[1], reverse=True)
    for dim, v in ordered:
        pct = max(0.0, min(1.0, float(v)))
        hi = "hi" if pct >= 0.6 else ""
        rows.append(
            f'<div class="dl">{DIM_ZH.get(dim, dim)}</div>'
            f'<div class="track"><div class="fill {hi}" style="width:{pct*100:.0f}%"></div></div>'
            f'<div class="pv">{pct*100:.0f}</div>'
        )
    return "".join(rows)


def cluster_card(c: dict) -> str:
    resid = c.get("test_residual_pp")
    wr = c.get("test_win_rate")
    ci = c.get("test_wr_ci95") or [None, None]
    tone = "good" if (resid or 0) > 0.5 else "bad" if (resid or 0) < -0.5 else "mid"
    resid_cls = "pos" if (resid or 0) > 0 else "neg"
    wr_txt = f"{wr*100:.1f}%" if wr is not None else "--"
    ci_txt = f"[{ci[0]*100:.1f}, {ci[1]*100:.1f}]" if ci and ci[0] is not None else ""
    chips = "".join(
        f'<span class="chip">{html.escape(str(r["champ"]))} <span class="lf">×{r["lift"]:.1f}</span></span>'
        for r in (c.get("rep_champions") or [])[:6]
    )
    return f"""<div class="card {tone}">
  <div class="chead">
    <div class="arch">#{c['id']} · {html.escape(ALL_HAND.get(c['nearest_hand_archetype'], c['nearest_hand_archetype']))}</div>
    <div class="sz">{c['size_frac_train']*100:.1f}% 隊伍</div>
  </div>
  <div class="stat">
    <div><span class="lbl">test 勝率</span><b>{wr_txt}</b> <span class="dim">{ci_txt}</span></div>
    <div><span class="lbl">殘差（扣英雄身分）</span><b class="{resid_cls}">{resid:+.2f}pp</b></div>
  </div>
  <div class="bars">{bar_rows(c['centroid_pct'])}</div>
  <div class="reps">{chips}</div>
</div>"""


def sweep_html(sweep: list, best_k: int) -> str:
    if not sweep:
        return ""
    sims = [r["silhouette"] for r in sweep]
    lo, hi = min(sims), max(sims)
    span = (hi - lo) or 1.0
    bars = []
    for r in sweep:
        h = 14 + 50 * (r["silhouette"] - lo) / span
        peak = "peak" if r["k"] == best_k else ""
        bars.append(f'<div class="b {peak}" style="height:{h:.0f}px" title="silhouette {r["silhouette"]:+.3f}"><span>{r["k"]}</span></div>')
    return f'<div class="sweep">{"".join(bars)}</div><div class="meta">silhouette by k（越高越「有分群」；峰值仍 &lt;0.25 = 連續體）</div>'


def render_split(data: dict, title: str, lead: bool) -> str:
    clusters = sorted(data["clusters"], key=lambda c: (c.get("test_residual_pp") is None, c.get("test_residual_pp")), reverse=True)
    matched = data.get("hand_archetypes_matched_by_a_cluster", [])
    never = [ALL_HAND.get(a, a) for a in ALL_HAND if a not in matched]
    verdict = ""
    if lead:
        verdict = f"""<div class="verdict">
  <div class="vcard"><div class="k">結構</div><div class="v">連續體</div>
    <div class="d">best silhouette {data['best_silhouette']:+.3f} @ k={data['best_silhouette_k']}；全 k &lt; 0.25 → 沒有乾淨的離散隊型</div></div>
  <div class="vcard"><div class="k">對齊手調軸</div><div class="v">ARI {data['k6_vs_hand_adjusted_rand']:+.2f}</div>
    <div class="d">NMI {data['k6_vs_hand_nmi']:+.2f}；6 群只對到 {data['n_distinct_hand_archetypes_matched']}/6 個手調原型</div></div>
  <div class="vcard"><div class="k">勝負訊號（殘差）</div><div class="v">{data['test_residual_spread_pp']:+.2f}pp</div>
    <div class="d">最佳−最差群的跨距，扣掉英雄身分後仍在；勝率跨距 {data['test_win_rate_spread_pp']:+.2f}pp</div></div>
</div>"""
    miss = f'<div class="note miss">沒有任何資料群以這些手調原型為最近鄰：<b>{"、".join(never)}</b> — 真實 Mayhem 隊伍幾乎不會「主要長成」續航或前後排型。</div>' if never else ""
    return f"""<section>
  <h2>{html.escape(title)}</h2>
  <div class="meta">{data['n_train_teams']:,} train / {data['n_test_teams']:,} test 隊伍 · split = {html.escape(data['split'])}</div>
  {verdict}
  {sweep_html(data.get('k_sweep', []), data.get('best_silhouette_k'))}
  {miss}
  <div class="grid" style="margin-top:14px">{"".join(cluster_card(c) for c in clusters)}</div>
</section>"""


@click.command()
@click.option("--cross", default=Path("outputs/ablation/ablation_team_archetype_clusters_crosspatch.json"),
              type=click.Path(path_type=Path), show_default=True)
@click.option("--within", default=Path("outputs/ablation/ablation_team_archetype_clusters.json"),
              type=click.Path(path_type=Path), show_default=True)
@click.option("--out", default=Path("documents/reports/team_archetype_clusters_review.html"),
              type=click.Path(path_type=Path), show_default=True)
def main(cross, within, out):
    sections = []
    if cross.exists():
        sections.append(render_split(json.loads(cross.read_text(encoding="utf-8")),
                                     "跨 patch 主結果（train 16.10+16.11 → test 16.12）", lead=True))
    if within.exists():
        sections.append(render_split(json.loads(within.read_text(encoding="utf-8")),
                                     "穩健性對照：單 patch 16.10 時間切分", lead=False))
    if not sections:
        raise click.ClickException("no input JSON found; run ablation_team_archetype_clusters.py first")

    doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Q2 · Team Archetype Clusters Review</title>
<style>{CSS}</style></head>
<body>
<header>
  <h1>Q2 — 資料驅動的隊伍原型分群</h1>
  <div class="meta">把真實 Mayhem 隊伍以 9 維能力向量分群，問：手調的 6 條 comp-fit 軸是不是「真的原型」？殘差 = 扣掉英雄身分後該群多贏/少贏多少。</div>
</header>
<main>
  <div class="note" style="margin-bottom:22px">
    <b>一句話：</b>隊伍組成是<b>連續體</b>，不是 6 個乾淨類型（silhouette 峰值在 k=2 且全 &lt; 0.25）。強迫切 6 群時，只對得上手調 6 軸中的 <b>4 個</b>（adc / dive / mage / poke）；<b>續航</b>與<b>前後排</b>從來不是任何資料群的主特徵。但群間勝負殘差有 ~3–5pp、且<b>跨 patch 穩定不縮水</b>：AP 傷害核心(+2.5)與 AP 開戰/控場(+1.2)超打，純消耗砲台(−2.4)與純坦開戰低打。→ 支持 Q3 的決定：comp-fit 當<b>唯讀軟性指標</b>，不要硬分類或接進推薦。
  </div>
  {"".join(sections)}
  <div class="note" style="margin-top:8px">
    殘差以 train 擬合的「英雄身分 LR」殘差（y − p̂）累計，blue +、red −，故已扣除可加的英雄強度；剩下的群間差是<b>非加性</b>組成效應。能力向量取自已部署 payload 的 <code>champ.comp</code>（與線上雷達同一套百分位軸）。
  </div>
</main>
</body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"written: {out}  ({len(sections)} section(s))")


if __name__ == "__main__":
    main()
