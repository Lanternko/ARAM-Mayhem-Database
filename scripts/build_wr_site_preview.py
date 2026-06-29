"""Build a site-like preview for the proposed win-rate display treatment."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from build_wr_display_demo import (  # type: ignore
    _build_rows,
    _collect_champion_stats,
    _load_payload,
)

ROOT = Path(__file__).resolve().parents[1]

TIER_ORDER = ("OP", "T1", "T2", "T3", "T4", "T5")
TIER_LABEL = {
    "OP": "OP",
    "T1": "T1",
    "T2": "T2",
    "T3": "T3",
    "T4": "T4",
    "T5": "T5",
}


def render_html(*, rows: list[dict], patch_prefix: str, queue_id: int, min_games: int, eb_k: float) -> str:
    data_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARAM Mayhem WR Display Site Preview</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #0e1116;
  --panel: #161a22;
  --card: #1f2530;
  --inner: #11151d;
  --text: #e6e8eb;
  --muted: #9aa0a6;
  --border: #30363d;
  --good: #6bd16b;
  --warn: #f5c518;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "Noto Sans TC", "Microsoft JhengHei UI", "Microsoft JhengHei", system-ui, sans-serif;
}}
.wrap {{
  max-width: 1260px;
  margin: 0 auto;
  padding: 24px 18px 42px;
}}
.topbar {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: end;
  margin-bottom: 16px;
}}
h1 {{
  margin: 0 0 6px;
  font-size: 22px;
  line-height: 1.2;
  letter-spacing: 0;
}}
.sub {{
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}}
.controls {{
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
}}
.seg {{
  display: inline-grid;
  grid-auto-flow: column;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #0b0e13;
  overflow: hidden;
}}
button {{
  appearance: none;
  border: 0;
  border-right: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  padding: 8px 10px;
  font: inherit;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}}
button:last-child {{ border-right: 0; }}
button.active {{
  color: #231802;
  background: #f5d780;
  font-weight: 800;
}}
.preview-note {{
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 0 0 18px;
}}
.note-card {{
  background: var(--panel);
  border: 1px solid #232936;
  border-radius: 10px;
  padding: 11px 12px;
  min-height: 58px;
}}
.note-card strong {{
  display: block;
  font-size: 13px;
  margin-bottom: 4px;
}}
.note-card span {{
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
}}
.tier-block {{
  margin-bottom: 22px;
  position: relative;
}}
.tier-heading {{
  margin: 0 0 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  border-bottom: 1px solid color-mix(in oklab, var(--tier-color) 30%, transparent);
  padding-bottom: 6px;
}}
.tier-pill {{
  min-width: 54px;
  display: inline-flex;
  justify-content: center;
  align-items: center;
  border-radius: 8px;
  padding: 4px 12px;
  font-size: 16px;
  font-weight: 800;
  color: var(--tier-fg, #11151d);
  background: var(--tier-bg);
}}
.tier-count {{
  color: var(--muted);
  font-size: 12px;
}}
.tier-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 10px;
}}
.champ {{
  position: relative;
  aspect-ratio: 1 / 1;
  border-radius: 8px;
  overflow: hidden;
  background: var(--card);
  border: 2px solid var(--tier-color);
  cursor: pointer;
  transition: transform .08s ease-out, filter .08s ease-out, box-shadow .08s ease-out;
}}
.champ:hover {{
  transform: translateY(-1px);
  filter: brightness(1.08);
}}
.tier-block[data-tier="OP"] .champ {{
  border-color: transparent;
  background:
    linear-gradient(#1f2530, #1f2530) padding-box,
    linear-gradient(135deg, #ffffff 0%, #e7d5ff 18%, #bcd6ff 36%, #ffd5ec 58%, #fff1c8 78%, #ffffff 100%) border-box;
  background-size: auto, 220% 220%;
  animation: prismShift 6s ease-in-out infinite;
  box-shadow: 0 0 8px rgba(220,180,255,0.45);
}}
.tier-block[data-tier="T1"] .champ {{
  border-color: transparent;
  background:
    linear-gradient(#1f2530, #1f2530) padding-box,
    linear-gradient(135deg, #ffb380 0%, #ff5a3c 32%, #c8262c 62%, #ff8050 100%) border-box;
  background-size: auto, 220% 220%;
  animation: prismShift 9s ease-in-out infinite;
  box-shadow: 0 0 6px rgba(255,90,60,0.42);
}}
@keyframes prismShift {{
  0% {{ background-position: 0% 50%; }}
  50% {{ background-position: 100% 50%; }}
  100% {{ background-position: 0% 50%; }}
}}
.champ img {{
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  border-radius: 6px;
}}
.raw-badge {{
  position: absolute;
  left: 3px;
  bottom: 18px;
  padding: 1px 4px;
  border-radius: 4px;
  background: rgba(14,17,22,.82);
  color: #f0f2f4;
  font-size: 10px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  z-index: 2;
}}
.conf-chip {{
  position: absolute;
  right: 3px;
  top: 3px;
  padding: 1px 4px;
  border-radius: 4px;
  border: 1px solid rgba(245,215,128,.45);
  background: rgba(9,14,22,.9);
  color: #f5d780;
  font-size: 9px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  z-index: 3;
}}
.name {{
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 12px 4px 3px;
  background: linear-gradient(to top, rgba(0,0,0,.86), rgba(0,0,0,0));
  color: #e6e8eb;
  font-size: 10px;
  font-weight: 650;
  line-height: 1.15;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.hover-sheet {{
  position: fixed;
  left: 18px;
  bottom: 18px;
  width: min(420px, calc(100vw - 36px));
  border: 1px solid rgba(255,255,255,.08);
  border-radius: 10px;
  background: #0b0e13;
  box-shadow: 0 12px 30px rgba(0,0,0,.45);
  padding: 12px;
  display: none;
  z-index: 20;
}}
.hover-sheet.show {{ display: block; }}
.hover-title {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}}
.hover-title strong {{ font-size: 15px; }}
.hover-title span {{ color: var(--muted); font-size: 11px; }}
.hover-grid {{
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}}
.hover-metric {{
  background: var(--inner);
  border: 1px solid rgba(255,255,255,.05);
  border-radius: 8px;
  padding: 8px;
}}
.hover-metric b {{
  display: block;
  font-size: 14px;
  margin-bottom: 2px;
  font-variant-numeric: tabular-nums;
}}
.hover-metric span {{
  color: var(--muted);
  font-size: 10px;
}}
@media (max-width: 760px) {{
  .wrap {{ padding: 16px 10px 30px; }}
  .topbar {{ grid-template-columns: 1fr; align-items: start; }}
  .controls {{ justify-content: stretch; }}
  .seg {{ width: 100%; grid-auto-columns: 1fr; }}
  .preview-note {{ grid-template-columns: 1fr; }}
  .tier-grid {{ grid-template-columns: repeat(6, 1fr); gap: 5px; }}
  .raw-badge {{ bottom: 15px; font-size: 9px; }}
  .conf-chip {{ font-size: 8px; }}
  .name {{ font-size: 9px; }}
  .hover-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>ARAM Mayhem 勝率顯示套用預覽</h1>
      <div class="sub">Patch {html.escape(patch_prefix)} · queue {queue_id} · min {min_games} 場 · EB k {eb_k:.1f}。主畫面顯示 raw WR，tier 用 EB 預估分組，tier 內用可信分數排序。</div>
    </div>
    <div class="controls">
      <div class="seg" id="confSeg">
        <button data-conf="80">80%</button>
        <button data-conf="90" class="active">90%</button>
        <button data-conf="95">95%</button>
      </div>
    </div>
  </div>
  <div class="preview-note">
    <div class="note-card"><strong>看得到的勝率：raw WR</strong><span>玩家直覺看到的百分比就是 wins / games，不把 EB mean 偽裝成勝率。</span></div>
    <div class="note-card"><strong>Tier：預估勝率</strong><span>OP/T1/T2 仍用勝率中心值分組，避免 lower bound 太保守時 T5 爆量。</span></div>
    <div class="note-card"><strong>排序：可信分數</strong><span>右上角 C90/C95 是保守下限，只用來排同 tier 裡的先後。</span></div>
    <div class="note-card"><strong>詳細資訊：hover 顯示</strong><span>滑過英雄會看到 raw、預估、可信分數、場次，讓資料口徑透明。</span></div>
  </div>
  <div id="tiers"></div>
</div>
<div class="hover-sheet" id="hoverSheet"></div>
<script>
const DATA = {data_json};
const TIER_ORDER = {json.dumps(TIER_ORDER)};
const TIER_STYLES = {{
  OP: {{ color: "#ffffff", bg: "linear-gradient(135deg,#ffffff,#e7d5ff,#bcd6ff,#ffd5ec,#fff1c8)", fg: "#11151d" }},
  T1: {{ color: "#ff5a3c", bg: "linear-gradient(135deg,#ffb380,#ff5a3c,#c8262c,#ff8050)", fg: "#1b0905" }},
  T2: {{ color: "#f5c518", bg: "#f5c518", fg: "#221900" }},
  T3: {{ color: "#8ec441", bg: "#8ec441", fg: "#101a07" }},
  T4: {{ color: "#3aa0ff", bg: "#3aa0ff", fg: "#06111d" }},
  T5: {{ color: "#7a7f8a", bg: "#7a7f8a", fg: "#11151d" }}
}};
let confidence = "90";

function pct(value) {{
  return (value * 100).toFixed(1) + "%";
}}

function tierFor(row) {{
  const score = row.estimate;
  if (score >= 0.55) return "OP";
  if (score >= 0.52) return "T1";
  if (score >= 0.50) return "T2";
  if (score >= 0.48) return "T3";
  if (score >= 0.46) return "T4";
  return "T5";
}}

function render() {{
  const grouped = new Map(TIER_ORDER.map(t => [t, []]));
  for (const row of DATA) {{
    grouped.get(tierFor(row)).push(row);
  }}
  for (const rows of grouped.values()) {{
    rows.sort((a, b) => b.confidence[confidence] - a.confidence[confidence] || b.raw - a.raw || b.games - a.games);
  }}
  document.getElementById("tiers").innerHTML = TIER_ORDER
    .filter(tier => grouped.get(tier).length)
    .map(tier => {{
      const rows = grouped.get(tier);
      const style = TIER_STYLES[tier];
      return `<section class="tier-block" data-tier="${{tier}}" style="--tier-color:${{style.color}};--tier-bg:${{style.bg}};--tier-fg:${{style.fg}};">
        <h2 class="tier-heading"><span class="tier-pill">${{tier}}</span><span class="tier-count">${{rows.length}} 隻 · 依 EB 預估分組，C${{confidence}} 排序</span></h2>
        <div class="tier-grid">
          ${{rows.map(row => `<button class="champ" type="button" data-id="${{row.id}}" title="${{row.name}} · raw ${{pct(row.raw)}} · C${{confidence}} ${{pct(row.confidence[confidence])}} · 預估 ${{pct(row.estimate)}} · ${{row.games}} 場">
            <img loading="lazy" src="${{row.image}}" alt="">
            <span class="conf-chip">C${{pct(row.confidence[confidence]).replace("%", "")}}</span>
            <span class="raw-badge">${{pct(row.raw)}}</span>
            <span class="name">${{row.name}}</span>
          </button>`).join("")}}
        </div>
      </section>`;
    }}).join("");
}}

function showHover(row) {{
  const sheet = document.getElementById("hoverSheet");
  sheet.innerHTML = `<div class="hover-title"><strong>${{row.name}}</strong><span>${{row.role || ""}} · ${{row.games}} 場</span></div>
    <div class="hover-grid">
      <div class="hover-metric"><b>${{pct(row.raw)}}</b><span>觀測勝率</span></div>
      <div class="hover-metric"><b>${{pct(row.estimate)}}</b><span>EB 預估</span></div>
      <div class="hover-metric"><b>${{pct(row.confidence[confidence])}}</b><span>C${{confidence}} 可信分數</span></div>
      <div class="hover-metric"><b>${{row.wins}} / ${{row.games}}</b><span>勝場 / 場次</span></div>
    </div>`;
  sheet.classList.add("show");
}}

document.getElementById("confSeg").addEventListener("click", event => {{
  const button = event.target.closest("button");
  if (!button) return;
  confidence = button.dataset.conf;
  for (const node of button.parentElement.querySelectorAll("button")) node.classList.remove("active");
  button.classList.add("active");
  render();
}});
document.addEventListener("pointerover", event => {{
  const champ = event.target.closest(".champ");
  if (!champ) return;
  const row = DATA.find(item => String(item.id) === champ.dataset.id);
  if (row) showHover(row);
}});
document.addEventListener("pointerout", event => {{
  if (!event.target.closest(".champ")) return;
  document.getElementById("hoverSheet").classList.remove("show");
}});
render();
</script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/lcu/games.db")
    parser.add_argument("--payload", type=Path, default=ROOT / "docs/api/tier-list.json")
    parser.add_argument("--queue-id", type=int, default=2400)
    parser.add_argument("--patch-prefix", default="16.12")
    parser.add_argument("--min-games", type=int, default=30)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/role_augment_analysis/site_wr_apply_preview.html")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_payload(args.payload)
    games, wins = _collect_champion_stats(args.db, queue_id=args.queue_id, patch_prefix=args.patch_prefix)
    rows, _global_mean, eb_k = _build_rows(payload=payload, games=games, wins=wins, min_games=args.min_games)
    args.out.write_text(
        render_html(
            rows=rows,
            patch_prefix=args.patch_prefix,
            queue_id=args.queue_id,
            min_games=args.min_games,
            eb_k=eb_k,
        ),
        encoding="utf-8",
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
