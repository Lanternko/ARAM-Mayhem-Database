"""Build a standalone win-rate display demo for Mayhem champion cards.

The demo intentionally separates:
- raw WR: the main user-facing win-rate number.
- estimate WR: empirical-Bayes posterior mean for analyst context.
- confidence score: one-sided Wilson lower bound used for sorting.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.champion_roles import PRIMARY_ROLE_OVERRIDES  # noqa: E402


CONFIDENCE_Z = {
    "80": 0.8416212336,
    "90": 1.2815515655,
    "95": 1.6448536270,
}


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _wilson_lower_bound(wins: int, games: int, z: float) -> float:
    if games <= 0:
        return 0.0
    p = wins / games
    z2 = z * z
    denom = 1 + z2 / games
    center = p + z2 / (2 * games)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * games)) / games)
    return max(0.0, (center - margin) / denom)


def _load_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_champion_stats(db_path: Path, *, queue_id: int, patch_prefix: str) -> tuple[Counter[int], Counter[int]]:
    games: Counter[int] = Counter()
    wins: Counter[int] = Counter()
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            """
            SELECT blue_champs, red_champs, blue_wins
            FROM games
            WHERE queue_id = ?
              AND patch LIKE ?
            """,
            (queue_id, f"{patch_prefix}%"),
        )
        for blue_json, red_json, blue_wins in rows:
            blue_won = bool(blue_wins)
            for champion_id in json.loads(blue_json):
                champion_id = int(champion_id)
                games[champion_id] += 1
                wins[champion_id] += int(blue_won)
            for champion_id in json.loads(red_json):
                champion_id = int(champion_id)
                games[champion_id] += 1
                wins[champion_id] += int(not blue_won)
    finally:
        con.close()
    return games, wins


def _estimate_empirical_bayes_k(rows: list[dict[str, Any]], *, global_mean: float) -> float:
    proportions = [row["raw"] for row in rows]
    games = [row["games"] for row in rows]
    if len(proportions) < 3:
        return 200.0
    observed_var = statistics.pvariance(proportions)
    average_noise = sum(global_mean * (1 - global_mean) / max(n, 1) for n in games) / len(games)
    true_var = max(observed_var - average_noise, 1e-6)
    k = global_mean * (1 - global_mean) / true_var - 1
    return min(max(k, 20.0), 2000.0)


def _build_rows(
    *,
    payload: dict[str, Any],
    games: Counter[int],
    wins: Counter[int],
    min_games: int,
) -> tuple[list[dict[str, Any]], float, float]:
    champ_meta = payload.get("champs") or {}
    total_games = sum(games.values())
    global_mean = (sum(wins.values()) / total_games) if total_games else 0.5

    rows: list[dict[str, Any]] = []
    for champion_id, game_count in games.items():
        if game_count < min_games:
            continue
        win_count = wins[champion_id]
        meta = champ_meta.get(str(champion_id), {})
        alias = meta.get("alias") or meta.get("name_en") or str(champion_id)
        row = {
            "id": champion_id,
            "name": meta.get("name_zh") or meta.get("name") or alias,
            "alias": alias,
            "role": PRIMARY_ROLE_OVERRIDES.get(alias) or (meta.get("tags") or [""])[0],
            "image": meta.get("image") or "",
            "games": int(game_count),
            "wins": int(win_count),
            "raw": win_count / game_count,
        }
        rows.append(row)

    eb_k = _estimate_empirical_bayes_k(rows, global_mean=global_mean)
    for row in rows:
        row["estimate"] = (row["wins"] + global_mean * eb_k) / (row["games"] + eb_k)
        row["confidence"] = {
            level: _wilson_lower_bound(row["wins"], row["games"], z)
            for level, z in CONFIDENCE_Z.items()
        }
    rows.sort(key=lambda row: row["confidence"]["90"], reverse=True)
    return rows, global_mean, eb_k


def _render_html(
    *,
    rows: list[dict[str, Any]],
    patch_prefix: str,
    queue_id: int,
    min_games: int,
    global_mean: float,
    eb_k: float,
) -> str:
    data_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    title = f"Mayhem {patch_prefix} WR Display Demo"
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #101114;
  --panel: #181a1f;
  --line: #2a2d34;
  --text: #f4f2ed;
  --muted: #aaa6a0;
  --accent: #62c7a2;
  --warn: #f0c95d;
  --bad: #e87979;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: "Microsoft JhengHei UI", "Noto Sans TC", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
}}
header {{
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgba(16, 17, 20, 0.94);
  border-bottom: 1px solid var(--line);
  backdrop-filter: blur(12px);
}}
.bar {{
  max-width: 1320px;
  margin: 0 auto;
  padding: 16px 20px;
  display: grid;
  grid-template-columns: minmax(260px, 1fr) auto auto;
  gap: 16px;
  align-items: center;
}}
h1 {{
  margin: 0;
  font-size: 20px;
  font-weight: 800;
  letter-spacing: 0;
}}
.meta {{
  color: var(--muted);
  font-size: 12px;
  margin-top: 4px;
}}
.seg {{
  display: inline-grid;
  grid-auto-flow: column;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: #0d0e11;
}}
button {{
  appearance: none;
  border: 0;
  border-right: 1px solid var(--line);
  background: transparent;
  color: var(--muted);
  padding: 9px 12px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
}}
button:last-child {{ border-right: 0; }}
button.active {{
  background: var(--accent);
  color: #06110d;
  font-weight: 800;
}}
main {{
  max-width: 1320px;
  margin: 0 auto;
  padding: 18px 20px 40px;
}}
.legend {{
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}}
.metric {{
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: var(--panel);
}}
.metric b {{
  display: block;
  font-size: 18px;
  margin-bottom: 4px;
}}
.metric span {{
  color: var(--muted);
  font-size: 12px;
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(188px, 1fr));
  gap: 10px;
}}
.card {{
  min-height: 118px;
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  padding: 10px;
}}
.card img {{
  width: 56px;
  height: 56px;
  border-radius: 8px;
  object-fit: cover;
  background: #0d0e11;
}}
.name {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}}
.name strong {{
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}}
.rank {{
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.wr {{
  font-size: 28px;
  line-height: 1.05;
  font-weight: 900;
  margin: 5px 0 8px;
  color: var(--text);
}}
.sub {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 5px 8px;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.sub span:nth-child(even) {{
  text-align: right;
  color: var(--text);
}}
.score-good {{ color: var(--accent) !important; }}
.score-warn {{ color: var(--warn) !important; }}
@media (max-width: 760px) {{
  .bar {{ grid-template-columns: 1fr; }}
  .legend {{ grid-template-columns: 1fr 1fr; }}
  .seg {{ width: 100%; grid-auto-columns: 1fr; }}
}}
</style>
</head>
<body>
<header>
  <div class="bar">
    <div>
      <h1>Mayhem {html.escape(patch_prefix)} 勝率顯示 Demo</h1>
      <div class="meta">主數字是 raw WR；排序可以切換。queue {queue_id}，min games {min_games}，EB k = {eb_k:.1f}</div>
    </div>
    <div class="seg" id="sortSeg" aria-label="排序">
      <button data-sort="confidence" class="active">可信排序</button>
      <button data-sort="raw">Raw WR</button>
      <button data-sort="estimate">預估值</button>
      <button data-sort="games">場次</button>
    </div>
    <div class="seg" id="confSeg" aria-label="信心水準">
      <button data-conf="80">80%</button>
      <button data-conf="90" class="active">90%</button>
      <button data-conf="95">95%</button>
    </div>
  </div>
</header>
<main>
  <section class="legend">
    <div class="metric"><b id="metricCount">0</b><span>納入英雄</span></div>
    <div class="metric"><b>{_pct(global_mean)}</b><span>全體平均，理論上接近 50%</span></div>
    <div class="metric"><b>Raw WR</b><span>卡片主顯示，直覺勝率</span></div>
    <div class="metric"><b>可信分數</b><span>Wilson lower bound，只用於排序</span></div>
  </section>
  <section class="grid" id="grid"></section>
</main>
<script>
const DATA = {data_json};
let sortMode = "confidence";
let confidence = "90";

function pct(value) {{
  return (value * 100).toFixed(1) + "%";
}}

function sortRows() {{
  const rows = [...DATA];
  rows.sort((a, b) => {{
    if (sortMode === "confidence") return b.confidence[confidence] - a.confidence[confidence] || b.raw - a.raw;
    if (sortMode === "raw") return b.raw - a.raw || b.games - a.games;
    if (sortMode === "estimate") return b.estimate - a.estimate || b.games - a.games;
    if (sortMode === "games") return b.games - a.games || b.raw - a.raw;
    return 0;
  }});
  return rows;
}}

function render() {{
  const grid = document.getElementById("grid");
  const rows = sortRows();
  document.getElementById("metricCount").textContent = rows.length;
  grid.innerHTML = rows.map((row, index) => {{
    const conf = row.confidence[confidence];
    const scoreClass = conf >= 0.55 ? "score-good" : (conf >= 0.52 ? "score-warn" : "");
    const img = row.image ? `<img src="${{row.image}}" alt="">` : `<div></div>`;
    return `<article class="card">
      ${{img}}
      <div>
        <div class="name"><strong title="${{row.name}}">${{row.name}}</strong><span class="rank">#${{index + 1}}</span></div>
        <div class="wr">${{pct(row.raw)}}</div>
        <div class="sub">
          <span>場次</span><span>${{row.games}}</span>
          <span>預估</span><span>${{pct(row.estimate)}}</span>
          <span>${{confidence}}%可信</span><span class="${{scoreClass}}">${{pct(conf)}}</span>
          <span>職業</span><span>${{row.role || "?"}}</span>
        </div>
      </div>
    </article>`;
  }}).join("");
}}

function wireSegment(id, key) {{
  document.getElementById(id).addEventListener("click", (event) => {{
    const button = event.target.closest("button");
    if (!button) return;
    for (const node of button.parentElement.querySelectorAll("button")) node.classList.remove("active");
    button.classList.add("active");
    if (key === "sort") sortMode = button.dataset.sort;
    if (key === "conf") confidence = button.dataset.conf;
    render();
  }});
}}

wireSegment("sortSeg", "sort");
wireSegment("confSeg", "conf");
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
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/role_augment_analysis/wr_display_demo_16_12.html")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_payload(args.payload)
    games, wins = _collect_champion_stats(
        args.db,
        queue_id=args.queue_id,
        patch_prefix=args.patch_prefix,
    )
    rows, global_mean, eb_k = _build_rows(
        payload=payload,
        games=games,
        wins=wins,
        min_games=args.min_games,
    )
    args.out.write_text(
        _render_html(
            rows=rows,
            patch_prefix=args.patch_prefix,
            queue_id=args.queue_id,
            min_games=args.min_games,
            global_mean=global_mean,
            eb_k=eb_k,
        ),
        encoding="utf-8",
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
