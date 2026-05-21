"""Build a static review page for semantic engage/wave rankings.

The page embeds champion semantic scores and lets a human reorder wave / engage
rankings in the browser.  Exported JSON is meant to become the feedback input
for future skill-level tuning.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any

import click

try:
    from champion_roles import ROLE_LABELS, ROLE_ORDER, role_definitions_payload, role_tags_for_alias
except ImportError:  # pragma: no cover - supports importing as scripts.build_semantic_score_review_page.
    from scripts.champion_roles import ROLE_LABELS, ROLE_ORDER, role_definitions_payload, role_tags_for_alias


DEFAULT_SCORE_CSV = Path("data/cache/champion_semantic_scores.csv")
DEFAULT_SKILL_DEBUG_CSV = Path("data/cache/champion_semantic_skill_debug.csv")
DEFAULT_ABILITY_JSON = Path("data/cache/champion_abilities.json")
DEFAULT_OUT = Path("docs/semantic-score-review.html")


def load_champion_meta(ability_json: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    raw = json.loads(ability_json.read_text(encoding="utf-8"))
    version = str(raw.get("version") or "latest")
    out: dict[str, dict[str, Any]] = {}
    for champion in raw.get("champions", []):
        alias = str(champion.get("alias") or "")
        if not alias:
            continue
        ddragon_tags = list(champion.get("tags") or [])
        role_tags = role_tags_for_alias(alias, ddragon_tags)
        out[alias] = {
            "champion_id": int(champion.get("champion_id") or 0),
            "name_en": champion.get("name_en") or alias,
            "name_zh": champion.get("name_zh") or "",
            "tags": role_tags,
            "ddragon_tags": ddragon_tags,
        }
    return version, out


def load_skill_debug(skill_debug_csv: Path) -> list[dict[str, Any]]:
    if not skill_debug_csv.exists():
        return []
    rows: list[dict[str, Any]] = []
    with skill_debug_csv.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                {
                    "champion_alias": raw.get("champion_alias") or "",
                    "champion_name_en": raw.get("champion_name_en") or raw.get("champion_alias") or "",
                    "champion_name_zh": raw.get("champion_name_zh") or "",
                    "spell": raw.get("spell_slot") or "",
                    "spell_name_en": raw.get("spell_name_en") or "",
                    "wave_score": float(raw.get("wave_skill_score") or 0.0),
                    "engage_score": float(raw.get("engage_skill_score") or 0.0),
                    "shape": raw.get("shape") or "",
                    "condition": raw.get("cast_state") or "",
                    "cc_type": raw.get("cc_type") or "",
                    "engage_gate": raw.get("engage_gate") or "",
                    "expected_targets": float(raw.get("expected_targets") or 0.0),
                    "damage_component": float(raw.get("damage_component") or 0.0),
                    "supports_wave": str(raw.get("supports_wave") or "").lower() == "true",
                    "is_wave_top3": str(raw.get("is_wave_top3") or "").lower() == "true",
                    "is_engage_top3": str(raw.get("is_engage_top3") or "").lower() == "true",
                }
            )
    return rows


def load_scores(score_csv: Path, ability_json: Path, skill_debug_csv: Path = DEFAULT_SKILL_DEBUG_CSV) -> dict[str, Any]:
    version, meta = load_champion_meta(ability_json)
    rows: list[dict[str, Any]] = []
    formula_version = ""
    with score_csv.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            alias = str(raw.get("champion_alias") or "")
            formula_version = formula_version or str(raw.get("semantic_formula_version") or "")
            m = meta.get(alias, {})
            raw_tags = (raw.get("tags") or "").split("|") if raw.get("tags") else m.get("ddragon_tags", [])
            role_tags = role_tags_for_alias(alias, raw_tags)
            rows.append(
                {
                    "champion_id": int(raw.get("champion_id") or m.get("champion_id") or 0),
                    "alias": alias,
                    "name_en": raw.get("champion_name_en") or m.get("name_en") or alias,
                    "name_zh": raw.get("champion_name_zh") or m.get("name_zh") or "",
                    "tags": role_tags or m.get("tags", []),
                    "wave": float(raw.get("wave_clear_score") or 0.0),
                    "engage": float(raw.get("engage_score") or 0.0),
                    "wave_top_spells": raw.get("wave_top_spells") or "",
                    "engage_top_spells": raw.get("engage_top_spells") or "",
                    "build_profile": raw.get("build_profile") or "",
                    "build_items": raw.get("build_items") or "",
                    "st_floor": float(raw.get("st_floor") or 0.0),
                    "notes": raw.get("notes") or "",
                    "image_url": (
                        f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{alias}.png"
                    ),
                }
            )
    rows.sort(key=lambda row: row["champion_id"])
    return {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "data_dragon_version": version,
        "semantic_formula_version": formula_version or "unknown",
        "role_order": list(ROLE_ORDER),
        "role_labels": ROLE_LABELS,
        "champions": rows,
        "skills": load_skill_debug(skill_debug_csv),
    }


def page_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Semantic Score Review</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f3ea;
      --ink: #20231f;
      --muted: #667064;
      --line: #d7d0c2;
      --panel: #fffdf7;
      --panel-2: #ece5d6;
      --accent: #0f766e;
      --accent-2: #b45309;
      --danger: #b42318;
      --shadow: 0 14px 40px rgba(34, 31, 24, 0.10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        linear-gradient(180deg, rgba(15,118,110,.08), transparent 24rem),
        repeating-linear-gradient(90deg, rgba(32,35,31,.035) 0 1px, transparent 1px 80px),
        var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, "Segoe UI", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid var(--line);
      background: rgba(246, 243, 234, .92);
      backdrop-filter: blur(14px);
    }}
    .bar {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      gap: 1rem;
      align-items: center;
      max-width: 1500px;
      margin: 0 auto;
      padding: 1rem clamp(1rem, 2vw, 2rem);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(1.25rem, 2vw, 2rem);
      line-height: 1.05;
      font-weight: 800;
    }}
    .meta {{
      margin-top: .25rem;
      color: var(--muted);
      font-size: .86rem;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: .5rem;
      justify-content: flex-end;
      align-items: center;
    }}
    input, button, textarea {{
      font: inherit;
    }}
    input[type="search"] {{
      width: min(32vw, 20rem);
      min-width: 12rem;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: .62rem .75rem;
      background: var(--panel);
      color: var(--ink);
    }}
    button {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: .6rem .75rem;
      background: var(--panel);
      color: var(--ink);
      cursor: pointer;
    }}
    button.primary {{
      border-color: #0f766e;
      background: #0f766e;
      color: white;
    }}
    button.warn {{
      border-color: rgba(180,83,9,.35);
      color: var(--accent-2);
    }}
    main {{
      max-width: 1500px;
      margin: 0 auto;
      padding: 1rem clamp(1rem, 2vw, 2rem) 2rem;
    }}
    .tabs {{
      display: flex;
      gap: .5rem;
      margin-bottom: 1rem;
    }}
    .tab {{
      min-width: 8rem;
      border-color: rgba(15,118,110,.22);
      background: rgba(255,253,247,.78);
      font-weight: 780;
    }}
    .tab.active {{
      border-color: #0f766e;
      background: #0f766e;
      color: #fff;
    }}
    .category-panel {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: .75rem;
      margin: -.35rem 0 1rem;
      padding: .75rem;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255,253,247,.76);
      box-shadow: 0 8px 22px rgba(34,31,24,.06);
    }}
    .category-label {{
      display: flex;
      gap: .5rem;
      align-items: baseline;
      white-space: nowrap;
    }}
    .category-label strong {{
      font-size: .95rem;
    }}
    .category-label span {{
      color: var(--muted);
      font-size: .82rem;
    }}
    .role-filters {{
      display: flex;
      flex-wrap: wrap;
      gap: .4rem;
      justify-content: flex-end;
    }}
    .role-filter {{
      padding: .42rem .58rem;
      border-radius: 999px;
      font-size: .84rem;
      font-weight: 760;
      background: rgba(255,255,255,.64);
    }}
    .role-filter.active {{
      border-color: #0f766e;
      background: #0f766e;
      color: #fff;
    }}
    .filter-count {{
      opacity: .68;
      margin-left: .18rem;
      font-variant-numeric: tabular-nums;
    }}
    .board {{
      display: block;
    }}
    .lane {{
      min-width: 0;
      border: 1px solid var(--line);
      background: rgba(255, 253, 247, .86);
      box-shadow: var(--shadow);
      display: none;
    }}
    .lane.active {{
      display: block;
    }}
    .lane-head {{
      position: sticky;
      top: 73px;
      z-index: 10;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: .75rem;
      align-items: center;
      padding: .9rem 1rem;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 253, 247, .95);
      backdrop-filter: blur(12px);
    }}
    .lane-title {{
      display: flex;
      gap: .55rem;
      align-items: baseline;
      min-width: 0;
    }}
    .lane-title strong {{
      font-size: 1.15rem;
    }}
    .lane-title span {{
      color: var(--muted);
      font-size: .85rem;
      white-space: nowrap;
    }}
    .list {{
      min-height: 16rem;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(178px, 1fr));
      gap: .65rem;
      padding: .75rem;
      align-items: stretch;
    }}
    .row {{
      position: relative;
      display: grid;
      grid-template-columns: 2.25rem 44px minmax(0, 1fr);
      grid-template-rows: auto auto;
      gap: .45rem .55rem;
      align-items: center;
      min-height: 6.9rem;
      padding: .62rem;
      border: 1px solid rgba(215,208,194,.86);
      border-radius: 8px;
      background: rgba(255,255,255,.58);
      box-shadow: 0 8px 18px rgba(34, 31, 24, .06);
    }}
    .row {{
      cursor: grab;
      user-select: none;
      touch-action: none;
    }}
    .row:hover {{
      border-color: rgba(15,118,110,.45);
    }}
    .row.dragging {{
      opacity: .38;
    }}
    .drag-proxy {{
      position: fixed;
      z-index: 1000;
      width: min(178px, calc(100vw - 2rem));
      pointer-events: none;
      transform: rotate(-1deg);
      box-shadow: 0 18px 48px rgba(34, 31, 24, .2);
    }}
    .row.drop-target {{
      outline: 3px solid rgba(15,118,110,.35);
      outline-offset: 2px;
    }}
    .row.changed {{
      background: rgba(15, 118, 110, .08);
    }}
    .row.noted::after {{
      content: "note";
      position: absolute;
      top: .45rem;
      right: .45rem;
      padding: .1rem .34rem;
      border-radius: 999px;
      background: rgba(180,83,9,.14);
      color: var(--accent-2);
      font-size: .68rem;
      font-weight: 800;
      letter-spacing: .02em;
      text-transform: uppercase;
    }}
    .rank {{
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      text-align: right;
    }}
    img.icon {{
      width: 44px;
      height: 44px;
      border-radius: 6px;
      object-fit: cover;
      background: var(--panel-2);
    }}
    .champ {{
      min-width: 0;
    }}
    .name {{
      display: flex;
      gap: .45rem;
      align-items: center;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-weight: 750;
    }}
    .sub {{
      color: var(--muted);
      font-size: .78rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: .08rem;
    }}
    .skills {{
      grid-column: 2 / -1;
      color: var(--ink);
      font-size: .88rem;
      font-weight: 760;
      letter-spacing: .01em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .score, .delta {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      font-weight: 720;
    }}
    .score {{
      grid-column: 1 / 3;
      justify-self: start;
      padding: .16rem .42rem;
      border-radius: 999px;
      background: rgba(32,35,31,.07);
    }}
    .score.manual {{
      background: rgba(15,118,110,.10);
      color: #075f59;
    }}
    .score-adjust {{
      margin-left: .28rem;
      font-size: .74rem;
      color: var(--accent);
    }}
    .score-adjust.negative {{
      color: var(--danger);
    }}
    .delta {{
      color: var(--muted);
      font-size: .85rem;
      justify-self: end;
    }}
    .delta.up {{ color: var(--accent); }}
    .delta.down {{ color: var(--danger); }}
    .empty-state {{
      grid-column: 1 / -1;
      padding: 2.4rem 1rem;
      text-align: center;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 10px;
      background: rgba(255,255,255,.48);
    }}
    .drawer {{
      margin-top: 1rem;
      border: 1px solid var(--line);
      background: rgba(255, 253, 247, .9);
      box-shadow: var(--shadow);
    }}
    .drawer summary {{
      cursor: pointer;
      padding: .9rem 1rem;
      font-weight: 750;
    }}
    textarea {{
      width: 100%;
      min-height: 16rem;
      border: 0;
      border-top: 1px solid var(--line);
      padding: 1rem;
      resize: vertical;
      background: #fbf8ef;
      color: var(--ink);
      font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
      font-size: .86rem;
    }}
    .hint {{
      color: var(--muted);
      padding: 0 1rem 1rem;
      font-size: .88rem;
    }}
    .skill-rank-body {{
      padding: 1rem;
      background: rgba(251,248,239,.62);
    }}
    .skill-rank-tools {{
      display: flex;
      flex-wrap: wrap;
      gap: .5rem;
      align-items: center;
      margin-bottom: .75rem;
    }}
    .skill-rank-tools select {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: .55rem .65rem;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
    }}
    .skill-rank-list {{
      max-height: min(64vh, 42rem);
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.56);
    }}
    .skill-rank-row {{
      display: grid;
      grid-template-columns: 3rem 2.25rem minmax(0, 1.05fr) minmax(0, 1fr) auto auto;
      gap: .65rem;
      align-items: center;
      padding: .55rem .7rem;
      border-bottom: 1px solid rgba(215,208,194,.72);
      font-size: .86rem;
    }}
    .skill-rank-row:last-child {{
      border-bottom: 0;
    }}
    .skill-rank-row strong,
    .skill-rank-row span {{
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .skill-rank-row small {{
      color: var(--muted);
    }}
    .skill-rank-icon {{
      width: 2.15rem;
      height: 2.15rem;
      border-radius: 7px;
      border: 1px solid rgba(32,35,31,.14);
      object-fit: cover;
      background: rgba(255,255,255,.72);
    }}
    .skill-rank-actions {{
      display: inline-flex;
      gap: .25rem;
      justify-self: end;
    }}
    .skill-rank-actions button {{
      min-width: 2rem;
      min-height: 2rem;
      padding: .25rem .42rem;
      border-radius: 7px;
      font-size: .78rem;
      font-weight: 800;
      line-height: 1;
    }}
    .skill-board {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: .9rem;
      padding: .75rem;
    }}
    .skill-category {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.54);
      overflow: hidden;
    }}
    .skill-category-head {{
      display: flex;
      justify-content: space-between;
      gap: .75rem;
      align-items: baseline;
      padding: .75rem .85rem;
      border-bottom: 1px solid var(--line);
      background: rgba(251,248,239,.76);
    }}
    .skill-category-head strong {{
      font-size: .95rem;
    }}
    .skill-category-head span {{
      color: var(--muted);
      font-size: .78rem;
      white-space: nowrap;
    }}
    .skill-category-list {{
      max-height: 32rem;
      overflow: auto;
    }}
    dialog {{
      width: min(42rem, calc(100vw - 2rem));
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 0;
      background: var(--panel);
      color: var(--ink);
      box-shadow: 0 24px 80px rgba(34,31,24,.22);
    }}
    dialog::backdrop {{
      background: rgba(32,35,31,.28);
      backdrop-filter: blur(2px);
    }}
    .note-dialog-head {{
      padding: 1rem 1rem .65rem;
      border-bottom: 1px solid var(--line);
    }}
    .note-dialog-head strong {{
      display: block;
      font-size: 1.05rem;
    }}
    .note-dialog-head span {{
      color: var(--muted);
      font-size: .86rem;
    }}
    .spell-review {{
      padding: .75rem 1rem .55rem;
      border-bottom: 1px solid var(--line);
      background: rgba(251,248,239,.62);
    }}
    .spell-review-title {{
      display: flex;
      justify-content: space-between;
      gap: .75rem;
      color: var(--muted);
      font-size: .82rem;
      margin-bottom: .55rem;
    }}
    .spell-review-list {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: .5rem;
    }}
    .spell-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.7);
      padding: .55rem;
      text-align: left;
      color: var(--ink);
      cursor: pointer;
    }}
    .spell-card.active {{
      border-color: rgba(15,118,110,.72);
      box-shadow: 0 0 0 3px rgba(15,118,110,.14);
    }}
    .spell-card.manual {{
      background: rgba(15,118,110,.09);
    }}
    .spell-key {{
      font-weight: 850;
      letter-spacing: .04em;
    }}
    .spell-meta {{
      color: var(--muted);
      font-size: .76rem;
      margin-top: .18rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .spell-score-line {{
      margin-top: .45rem;
      font-variant-numeric: tabular-nums;
      font-weight: 780;
    }}
    .spell-adjust {{
      margin-left: .35rem;
      color: var(--accent);
      font-size: .78rem;
    }}
    .spell-adjust.negative {{
      color: var(--danger);
    }}
    .spell-review-controls {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: .5rem;
      margin-top: .55rem;
      color: var(--muted);
      font-size: .8rem;
    }}
    .spell-review-controls .buttons {{
      display: flex;
      gap: .35rem;
    }}
    .mini-btn {{
      min-width: 5.4rem;
      min-height: 2.25rem;
      padding: .42rem .7rem;
      border-radius: 8px;
      font-size: .84rem;
      font-weight: 780;
      user-select: none;
      touch-action: none;
    }}
    #noteText {{
      min-height: 11rem;
      border-top: 0;
      background: #fbf8ef;
    }}
    .note-actions {{
      display: flex;
      justify-content: flex-end;
      gap: .5rem;
      padding: .75rem 1rem 1rem;
    }}
    @media (max-width: 980px) {{
      .bar {{
        grid-template-columns: 1fr;
      }}
      .controls {{
        justify-content: flex-start;
      }}
      .category-panel {{
        align-items: flex-start;
        flex-direction: column;
      }}
      .role-filters {{
        justify-content: flex-start;
      }}
      input[type="search"] {{
        width: 100%;
      }}
      .lane-head {{
        top: 127px;
      }}
      .list {{
        grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
        padding: .55rem;
      }}
      .row {{
        grid-template-columns: 2rem 38px minmax(0, 1fr);
        min-height: 7.5rem;
      }}
      img.icon {{
        width: 38px;
        height: 38px;
      }}
      .delta {{
        display: none;
      }}
      .spell-review-list {{
        grid-template-columns: 1fr;
      }}
      .skill-board {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div>
        <h1>Semantic Score Review</h1>
        <div class="meta" id="meta"></div>
      </div>
      <div class="controls">
        <input id="search" type="search" placeholder="Search champion" />
        <button id="exportBtn" class="primary">Export ranking</button>
        <button id="resetBtn" class="warn">Reset local edits</button>
      </div>
    </div>
  </header>
  <main>
    <nav class="tabs" aria-label="Score metric">
      <button class="tab active" data-tab="wave" type="button">Wave</button>
      <button class="tab" data-tab="engage" type="button">Engage</button>
      <button class="tab" data-tab="skill-ranking" type="button">Skill ranking</button>
    </nav>
    <section class="category-panel" aria-label="Champion category filter">
      <div class="category-label">
        <strong>分類</strong>
        <span id="roleFilterSummary">All champions</span>
      </div>
      <div id="roleFilters" class="role-filters"></div>
    </section>
    <section class="board">
      <section class="lane active" data-metric="wave">
        <div class="lane-head">
          <div class="lane-title"><strong>Wave Score</strong><span>drag to reorder · double-click note</span></div>
          <div class="score">Score</div>
        </div>
        <div id="waveList" class="list"></div>
      </section>
      <section class="lane" data-metric="engage">
        <div class="lane-head">
          <div class="lane-title"><strong>Engage Score</strong><span>drag to reorder · double-click note</span></div>
          <div class="score">Score</div>
        </div>
        <div id="engageList" class="list"></div>
      </section>
      <section class="lane" data-metric="skill-ranking">
        <div class="lane-head">
          <div class="lane-title"><strong>Skill Ranking</strong><span>adjusted by current browser edits</span></div>
          <div class="score">Score</div>
        </div>
        <div id="skillRankingBoard" class="skill-board"></div>
      </section>
    </section>
    <details class="drawer" open>
      <summary>Ranking export</summary>
      <div class="hint">調整排名後按 Export ranking。把這段 JSON 貼回來，我就能依照你的排序回推要調哪些技能強度。</div>
      <textarea id="exportBox" spellcheck="false"></textarea>
    </details>
    <dialog id="noteDialog">
      <div class="note-dialog-head">
        <strong id="noteTitle">Champion note</strong>
        <span id="noteSubtitle">Double-click a card to edit notes.</span>
      </div>
      <section class="spell-review" aria-label="Spell score review">
        <div class="spell-review-title">
          <strong id="spellReviewTitle">Top spell scores</strong>
          <span id="spellReviewHint">Click a spell, then use ↑ / ↓ to tune it.</span>
        </div>
        <div id="spellReviewList" class="spell-review-list"></div>
        <div class="spell-review-controls">
          <span id="spellReviewStatus">No manual spell adjustment.</span>
          <div class="buttons">
            <button id="spellDownBtn" class="mini-btn" type="button">↓ -0.05</button>
            <button id="spellResetBtn" class="mini-btn warn" type="button">Reset skill</button>
            <button id="spellUpBtn" class="mini-btn" type="button">↑ +0.05</button>
          </div>
        </div>
      </section>
      <textarea id="noteText" spellcheck="false" placeholder="寫下你覺得這隻英雄排名該怎麼調、哪個技能被高估/低估..."></textarea>
      <div class="note-actions">
        <button id="noteDeleteBtn" class="warn" type="button">Clear note</button>
        <button id="noteCancelBtn" type="button">Cancel</button>
        <button id="noteSaveBtn" class="primary" type="button">Save note</button>
      </div>
    </dialog>
  </main>
  <script>
    const DATA = {data_json};
    const METRICS = [
      {{ key: "wave", listId: "waveList", spellKey: "wave_top_spells", scoreLabel: "wave_clear_score", abilitySection: "wave_clear" }},
      {{ key: "engage", listId: "engageList", spellKey: "engage_top_spells", scoreLabel: "engage_score", abilitySection: "engage" }},
    ];
    const TOP3_WEIGHTS = {{
      wave: [0.55, 0.30, 0.15],
      engage: [0.60, 0.24, 0.10],
    }};
    const SKILL_RANK_CATEGORIES = [
      {{ key: "aoe_control", label: "群體控制", hint: "AOE hard CC / displacement" }},
      {{ key: "aoe_slow", label: "群體緩速", hint: "AOE soft slow" }},
      {{ key: "single_control", label: "單體控制", hint: "single-target hard CC / hook" }},
      {{ key: "single_slow", label: "單體緩速", hint: "single-target soft slow" }},
      {{ key: "mobility", label: "位移", hint: "pure dash / no CC" }},
      {{ key: "damage", label: "傷害", hint: "damage pressure without CC" }},
    ];
    const ROLE_FILTERS = DATA.role_order || ["Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank"];
    const ROLE_LABELS = DATA.role_labels || {{}};
    const stateKey = `semantic-score-review:${{DATA.semantic_formula_version || "v1"}}`;
    const modelOrder = {{}};
    const state = loadState();
    let dragging = null;
    let dragCandidate = null;
    let dragProxy = null;
    let dragHoverAlias = null;
    let lastPointer = {{ x: 0, y: 0 }};
    let activeMetric = localStorage.getItem("semantic-score-review:active-tab") || "wave";
    let activeRole = localStorage.getItem("semantic-score-review:role-filter") || "";
    let noteTarget = null;
    let activeSpellIndex = 0;

    function loadState() {{
      try {{
        const saved = JSON.parse(localStorage.getItem(stateKey) || "{{}}");
        const notes = saved.notes && typeof saved.notes === "object" ? saved.notes : {{}};
        const spellAdjustments = saved.spellAdjustments && typeof saved.spellAdjustments === "object"
          ? saved.spellAdjustments
          : {{}};
        return {{
          wave: Array.isArray(saved.wave) ? saved.wave : null,
          engage: Array.isArray(saved.engage) ? saved.engage : null,
          notes: {{
            wave: notes.wave && typeof notes.wave === "object" ? notes.wave : {{}},
            engage: notes.engage && typeof notes.engage === "object" ? notes.engage : {{}},
          }},
          spellAdjustments: {{
            wave: spellAdjustments.wave && typeof spellAdjustments.wave === "object" ? spellAdjustments.wave : {{}},
            engage: spellAdjustments.engage && typeof spellAdjustments.engage === "object" ? spellAdjustments.engage : {{}},
          }},
        }};
      }} catch {{
        return {{
          wave: null,
          engage: null,
          notes: {{ wave: {{}}, engage: {{}} }},
          spellAdjustments: {{ wave: {{}}, engage: {{}} }},
        }};
      }}
    }}

    function saveState() {{
      localStorage.setItem(stateKey, JSON.stringify(state));
    }}

    function defaultOrder(metric) {{
      return [...DATA.champions]
        .sort((a, b) => b[metric] - a[metric] || a.name_en.localeCompare(b.name_en))
        .map(c => c.alias);
    }}

    function scoreOrder(metric) {{
      return [...DATA.champions]
        .sort((a, b) => adjustedMetricScore(b, metric) - adjustedMetricScore(a, metric) || a.name_en.localeCompare(b.name_en))
        .map(c => c.alias);
    }}

    function champion(alias) {{
      return DATA.champions.find(c => c.alias === alias);
    }}

    function getNote(metric, alias) {{
      return ((state.notes && state.notes[metric]) || {{}})[alias] || "";
    }}

    function setNote(metric, alias, note) {{
      state.notes = state.notes || {{ wave: {{}}, engage: {{}} }};
      state.notes[metric] = state.notes[metric] || {{}};
      const trimmed = note.trim();
      if (trimmed) {{
        state.notes[metric][alias] = trimmed;
      }} else {{
        delete state.notes[metric][alias];
      }}
      saveState();
    }}

    function metricConfig(metric) {{
      return METRICS.find(item => item.key === metric) || METRICS[0];
    }}

    function getSpellAdjustments(metric, alias) {{
      state.spellAdjustments = state.spellAdjustments || {{ wave: {{}}, engage: {{}} }};
      state.spellAdjustments[metric] = state.spellAdjustments[metric] || {{}};
      state.spellAdjustments[metric][alias] = state.spellAdjustments[metric][alias] || {{}};
      return state.spellAdjustments[metric][alias];
    }}

    function setSpellAdjustment(metric, alias, spell, value) {{
      const bucket = getSpellAdjustments(metric, alias);
      const rounded = Number(value.toFixed(2));
      if (Math.abs(rounded) < 0.001) {{
        delete bucket[spell];
      }} else {{
        bucket[spell] = rounded;
      }}
      if (!Object.keys(bucket).length) {{
        delete state.spellAdjustments[metric][alias];
      }}
      saveState();
    }}

    function getSpellAdjustment(metric, alias, spell) {{
      const value = (((state.spellAdjustments || {{}})[metric] || {{}})[alias] || {{}})[spell];
      return Number.isFinite(value) ? value : 0;
    }}

    function validOrder(metric) {{
      const aliases = new Set(DATA.champions.map(c => c.alias));
      const current = (state[metric] || []).filter(alias => aliases.has(alias));
      const seen = new Set(current);
      for (const alias of defaultOrder(metric)) {{
        if (!seen.has(alias)) current.push(alias);
      }}
      state[metric] = current;
      return current;
    }}

    function rankMap(order) {{
      const out = new Map();
      order.forEach((alias, idx) => out.set(alias, idx + 1));
      return out;
    }}

    function formatSpellChain(raw) {{
      const labels = {{
        P: "被動",
        Passive: "被動",
        passive: "被動",
      }};
      return String(raw || "")
        .split(",")
        .map(part => part.trim())
        .filter(Boolean)
        .slice(0, 3)
        .map(part => labels[part] || part)
        .join(" > ");
    }}

    function parseAbilityBreakdown(c, metricKey) {{
      const metric = metricConfig(metricKey);
      const sections = {{}};
      String(c.notes || "").split(";").forEach(part => {{
        const [rawKey, rawValue] = part.split("=");
        const key = (rawKey || "").trim();
        if (!key || rawValue === undefined) return;
        sections[key] = String(rawValue || "").trim();
      }});
      const bySpell = new Map();
      String(sections[metric.abilitySection] || "").split(",").forEach(rawEntry => {{
        const parts = rawEntry.trim().split(":");
        if (parts.length < 2) return;
        const spell = parts[0].trim();
        const score = Number(parts[parts.length - 1]);
        if (!spell || !Number.isFinite(score)) return;
        bySpell.set(spell, {{
          spell,
          shape: parts.slice(1, -2).join(":") || parts[1] || "",
          condition: parts.length >= 3 ? parts[parts.length - 2] : "",
          baseScore: score,
        }});
      }});
      const topSpells = String(c[metric.spellKey] || "")
        .split(",")
        .map(part => part.trim())
        .filter(Boolean)
        .slice(0, 3);
      const rows = topSpells.map(spell => bySpell.get(spell) || {{
        spell,
        shape: "",
        condition: "",
        baseScore: 0,
      }});
      for (const item of bySpell.values()) {{
        if (rows.length >= 3) break;
        if (!rows.some(row => row.spell === item.spell)) rows.push(item);
      }}
      return rows.slice(0, 3);
    }}

    function adjustedAbilityRows(c, metricKey) {{
      return parseAbilityBreakdown(c, metricKey)
        .map(row => ({{
          ...row,
          adjustment: getSpellAdjustment(metricKey, c.alias, row.spell),
          adjustedScore: clampScoreFloor(
            row.baseScore + getSpellAdjustment(metricKey, c.alias, row.spell)
          ),
        }}))
        .sort((a, b) => b.adjustedScore - a.adjustedScore || a.spell.localeCompare(b.spell));
    }}

    function adjustedSpellChain(c, metricKey) {{
      return adjustedAbilityRows(c, metricKey).map(row => row.spell).join(",");
    }}

    function metricScoreAdjustment(c, metricKey) {{
      if (!c) return 0;
      const rows = parseAbilityBreakdown(c, metricKey);
      const weights = TOP3_WEIGHTS[metricKey] || [1, 0, 0];
      const baseWeighted = rows.reduce(
        (sum, row, idx) => sum + (weights[idx] || 0) * row.baseScore,
        0
      );
      const adjustedRows = adjustedAbilityRows(c, metricKey);
      const adjustedWeighted = adjustedRows.reduce(
        (sum, row, idx) => sum + (weights[idx] || 0) * row.adjustedScore,
        0
      );
      return adjustedWeighted - baseWeighted;
    }}

    function adjustedMetricScore(c, metricKey) {{
      return Number(c[metricKey] || 0) + metricScoreAdjustment(c, metricKey);
    }}

    function clampScoreFloor(value) {{
      return Math.max(0, Number(value || 0));
    }}

    function formatScore(value) {{
      return Number(value || 0).toFixed(2);
    }}

    function signedScore(value) {{
      if (!value) return "";
      return `${{value > 0 ? "+" : ""}}${{formatScore(value)}}`;
    }}

    function roleLabel(role) {{
      const labels = ROLE_LABELS[role] || {{}};
      return role ? (labels.zh || labels.en || role) : "All";
    }}

    function roleFilterApplies() {{
      return activeMetric !== "skill-ranking";
    }}

    function matchesRole(c) {{
      return !roleFilterApplies() || !activeRole || (c.tags || []).includes(activeRole);
    }}

    function visibleChampionCount() {{
      const query = document.getElementById("search").value.trim().toLowerCase();
      return DATA.champions.filter(c => {{
        if (!matchesRole(c)) return false;
        const haystack = `${{c.alias}} ${{c.name_en}} ${{c.name_zh}} ${{c.tags.join(" ")}}`.toLowerCase();
        return !query || haystack.includes(query);
      }}).length;
    }}

    function renderRoleFilters() {{
      const host = document.getElementById("roleFilters");
      const roles = ["", ...ROLE_FILTERS];
      const applies = roleFilterApplies();
      host.innerHTML = roles.map(role => {{
        const count = role ? DATA.champions.filter(c => (c.tags || []).includes(role)).length : DATA.champions.length;
        return `
          <button class="role-filter ${{applies && activeRole === role ? "active" : ""}}" type="button" data-role="${{role}}">
            ${{roleLabel(role)}}<span class="filter-count">${{count}}</span>
          </button>
        `;
      }}).join("");
      host.querySelectorAll(".role-filter").forEach(button => {{
        button.addEventListener("click", () => {{
          activeRole = button.dataset.role || "";
          localStorage.setItem("semantic-score-review:role-filter", activeRole);
          render();
        }});
      }});
      const shown = visibleChampionCount();
      const label = !applies ? "Skill ranking / All champions" : activeRole ? `${{roleLabel(activeRole)}} / ${{activeRole}}` : "All champions";
      document.getElementById("roleFilterSummary").textContent = `${{label}} - showing ${{shown}}`;
    }}

    function render() {{
      document.getElementById("meta").textContent =
        `${{DATA.champions.length}} champions · DDragon ${{DATA.data_dragon_version}} · generated ${{DATA.generated_at}}`;
      renderRoleFilters();
      document.querySelectorAll(".tab").forEach(tab => {{
        tab.classList.toggle("active", tab.dataset.tab === activeMetric);
      }});
      document.querySelectorAll(".lane").forEach(lane => {{
        lane.classList.toggle("active", lane.dataset.metric === activeMetric);
      }});
      for (const metric of METRICS) {{
        modelOrder[metric.key] = modelOrder[metric.key] || defaultOrder(metric.key);
        if (!state[metric.key]) state[metric.key] = scoreOrder(metric.key);
        renderList(metric);
      }}
      renderSkillRankingPage();
      updateExport();
    }}

    function renderList(metric) {{
      const list = document.getElementById(metric.listId);
      const query = document.getElementById("search").value.trim().toLowerCase();
      const order = scoreOrder(metric.key);
      const modelRanks = rankMap(modelOrder[metric.key]);
      list.innerHTML = "";
      let visibleRank = 0;
      order.forEach((alias, idx) => {{
        const c = champion(alias);
        if (!matchesRole(c)) return;
        const haystack = `${{c.alias}} ${{c.name_en}} ${{c.name_zh}} ${{c.tags.join(" ")}}`.toLowerCase();
        if (query && !haystack.includes(query)) return;
        visibleRank += 1;
        const manualRank = idx + 1;
        const modelRank = modelRanks.get(alias);
        const delta = modelRank - manualRank;
        const note = getNote(metric.key, alias);
        const topSpells = adjustedSpellChain(c, metric.key) || c[metric.spellKey] || "";
        const scoreAdjustment = metricScoreAdjustment(c, metric.key);
        const finalScore = adjustedMetricScore(c, metric.key);
        const row = document.createElement("div");
        row.className = "row" + (delta ? " changed" : "") + (note ? " noted" : "") + (scoreAdjustment ? " score-adjusted" : "");
        row.dataset.alias = alias;
        row.dataset.metric = metric.key;
        row.title = note ? `Double-click to edit note: ${{note}}` : "Double-click to add note";
        row.innerHTML = `
          <div class="rank" title="overall rank ${{manualRank}}">${{visibleRank}}</div>
          <img class="icon" src="${{c.image_url}}" alt="" loading="lazy" />
          <div class="champ">
            <div class="name"><span>${{c.name_en}}</span></div>
            <div class="sub">${{c.build_profile || c.tags.join("/")}}</div>
          </div>
          <div class="skills">${{formatSpellChain(topSpells) || "no major skill"}}</div>
          <div class="score ${{scoreAdjustment ? "manual" : ""}}">
            ${{formatScore(finalScore)}}${{scoreAdjustment ? `<span class="score-adjust ${{scoreAdjustment < 0 ? "negative" : ""}}">${{signedScore(scoreAdjustment)}}</span>` : ""}}
          </div>
          <div class="delta ${{delta > 0 ? "up" : delta < 0 ? "down" : ""}}">${{delta ? (delta > 0 ? "+" : "") + delta : ""}}</div>
        `;
        row.addEventListener("pointerdown", onPointerDown);
        row.addEventListener("dblclick", () => openNoteDialog(metric.key, alias));
        list.appendChild(row);
      }});
      if (!visibleRank) {{
        list.innerHTML = `<div class="empty-state">No champions match this category/search filter.</div>`;
      }}
    }}

    function onPointerDown(event) {{
      if (event.button !== 0) return;
      if (event.target.closest("button, input, textarea, dialog")) return;
      const row = event.currentTarget;
      dragCandidate = {{
        metric: row.dataset.metric,
        alias: row.dataset.alias,
        row,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        offsetX: event.clientX - row.getBoundingClientRect().left,
        offsetY: event.clientY - row.getBoundingClientRect().top,
      }};
      lastPointer = {{ x: event.clientX, y: event.clientY }};
      row.setPointerCapture(event.pointerId);
    }}

    function beginPointerDrag(clientX, clientY) {{
      if (!dragCandidate || dragging) return;
      dragging = {{
        metric: dragCandidate.metric,
        alias: dragCandidate.alias,
        offsetX: dragCandidate.offsetX,
        offsetY: dragCandidate.offsetY,
      }};
      dragCandidate.row.classList.add("dragging");
      dragProxy = dragCandidate.row.cloneNode(true);
      dragProxy.classList.add("drag-proxy");
      dragProxy.classList.remove("drop-target");
      dragProxy.style.width = `${{dragCandidate.row.getBoundingClientRect().width}}px`;
      document.body.appendChild(dragProxy);
      updateDragProxy(clientX, clientY);
      updateDropTarget(clientX, clientY);
    }}

    function onPointerMove(event) {{
      lastPointer = {{ x: event.clientX, y: event.clientY }};
      if (!dragging && dragCandidate) {{
        const dx = event.clientX - dragCandidate.startX;
        const dy = event.clientY - dragCandidate.startY;
        if (Math.hypot(dx, dy) > 5) beginPointerDrag(event.clientX, event.clientY);
      }}
      if (!dragging) return;
      event.preventDefault();
      updateDragProxy(event.clientX, event.clientY);
      updateDropTarget(event.clientX, event.clientY);
      autoScrollAtEdge(event.clientY);
    }}

    function onPointerUp(event) {{
      if (dragging && dragHoverAlias && dragHoverAlias !== dragging.alias) {{
        reorderAlias(dragging.metric, dragging.alias, dragHoverAlias);
      }}
      cleanupPointerDrag();
    }}

    function onPointerCancel() {{
      cleanupPointerDrag();
    }}

    function cleanupPointerDrag() {{
      if (dragCandidate?.row) {{
        dragCandidate.row.classList.remove("dragging");
        try {{
          dragCandidate.row.releasePointerCapture(dragCandidate.pointerId);
        }} catch {{}}
      }}
      if (dragProxy) dragProxy.remove();
      document.querySelectorAll(".row.drop-target").forEach(row => row.classList.remove("drop-target"));
      dragging = null;
      dragCandidate = null;
      dragProxy = null;
      dragHoverAlias = null;
    }}

    function updateDragProxy(clientX, clientY) {{
      if (!dragProxy || !dragging) return;
      dragProxy.style.left = `${{clientX - dragging.offsetX}}px`;
      dragProxy.style.top = `${{clientY - dragging.offsetY}}px`;
    }}

    function updateDropTarget(clientX, clientY) {{
      if (!dragging) return;
      const target = document
        .elementFromPoint(clientX, clientY)
        ?.closest(`.row[data-metric="${{dragging.metric}}"]`);
      document.querySelectorAll(".row.drop-target").forEach(row => row.classList.remove("drop-target"));
      if (target && target.dataset.alias !== dragging.alias) {{
        target.classList.add("drop-target");
        dragHoverAlias = target.dataset.alias;
      }} else {{
        dragHoverAlias = null;
      }}
    }}

    function autoScrollAtEdge(clientY) {{
      if (!dragging) return;
      const edge = 110;
      const maxStep = 34;
      let dy = 0;
      if (clientY < edge) {{
        dy = -maxStep * (1 - clientY / edge);
      }} else if (window.innerHeight - clientY < edge) {{
        dy = maxStep * (1 - (window.innerHeight - clientY) / edge);
      }}
      if (dy) {{
        window.scrollBy({{ top: dy, behavior: "auto" }});
        updateDropTarget(lastPointer.x, lastPointer.y);
      }}
    }}

    function onWheelDuringDrag(event) {{
      if (!dragging && dragCandidate) beginPointerDrag(lastPointer.x, lastPointer.y);
      if (!dragging) return;
      event.preventDefault();
      window.scrollBy({{
        top: event.deltaY,
        left: event.deltaX,
        behavior: "auto",
      }});
      updateDragProxy(lastPointer.x, lastPointer.y);
      updateDropTarget(lastPointer.x, lastPointer.y);
    }}

    function reorderAlias(metric, fromAlias, toAlias) {{
      const order = validOrder(metric);
      const from = order.indexOf(fromAlias);
      const to = order.indexOf(toAlias);
      if (from < 0 || to < 0 || from === to) return;
      order.splice(from, 1);
      order.splice(to, 0, fromAlias);
      saveState();
      render();
    }}

    function moveAlias(metric, alias, step) {{
      const order = validOrder(metric);
      const idx = order.indexOf(alias);
      const next = Math.max(0, Math.min(order.length - 1, idx + step));
      if (idx === next) return;
      order.splice(idx, 1);
      order.splice(next, 0, alias);
      saveState();
      render();
    }}

    function openNoteDialog(metric, alias) {{
      const c = champion(alias);
      noteTarget = {{ metric, alias }};
      activeSpellIndex = 0;
      document.getElementById("noteTitle").textContent = `${{c.name_en}} - ${{metric.toUpperCase()}} note`;
      document.getElementById("noteSubtitle").textContent =
        `${{c.name_zh || c.alias}} - ↑/↓ tune selected skill, Enter saves, Shift+Enter adds a new line`;
      document.getElementById("noteText").value = getNote(metric, alias);
      renderSpellReview();
      const dialog = document.getElementById("noteDialog");
      dialog.showModal();
      setTimeout(() => document.getElementById("noteText").focus(), 0);
    }}

    function renderSpellReview() {{
      if (!noteTarget) return;
      const c = champion(noteTarget.alias);
      const rows = adjustedAbilityRows(c, noteTarget.metric);
      activeSpellIndex = Math.max(0, Math.min(rows.length - 1, activeSpellIndex));
      const list = document.getElementById("spellReviewList");
      list.innerHTML = rows.map((row, idx) => {{
        const adjust = row.adjustment || 0;
        const finalScore = row.adjustedScore;
        const meta = [row.shape, row.condition].filter(Boolean).join(" / ") || "manual review";
        return `
          <button class="spell-card ${{idx === activeSpellIndex ? "active" : ""}} ${{adjust ? "manual" : ""}}"
            type="button" data-spell-index="${{idx}}" aria-pressed="${{idx === activeSpellIndex ? "true" : "false"}}">
            <div class="spell-key">${{row.spell}}</div>
            <div class="spell-meta">${{meta}}</div>
            <div class="spell-score-line">
              ${{formatScore(finalScore)}}
              ${{adjust ? `<span class="spell-adjust ${{adjust < 0 ? "negative" : ""}}">${{signedScore(adjust)}}</span>` : ""}}
            </div>
          </button>
        `;
      }}).join("");
      list.querySelectorAll(".spell-card").forEach(button => {{
        button.addEventListener("click", () => {{
          activeSpellIndex = Number(button.dataset.spellIndex || 0);
          renderSpellReview();
        }});
      }});
      const active = rows[activeSpellIndex];
      const totalAdjust = metricScoreAdjustment(c, noteTarget.metric);
      document.getElementById("spellReviewStatus").textContent = active
        ? `Selected ${{active.spell}} · weighted score delta ${{signedScore(totalAdjust) || "0.00"}}`
        : "No spell score data.";
    }}

    function adjustActiveSpell(step) {{
      if (!noteTarget) return;
      const c = champion(noteTarget.alias);
      const rows = adjustedAbilityRows(c, noteTarget.metric);
      const active = rows[activeSpellIndex];
      if (!active) return;
      const current = getSpellAdjustment(noteTarget.metric, noteTarget.alias, active.spell);
      setSpellAdjustment(noteTarget.metric, noteTarget.alias, active.spell, current + step);
      renderSpellReview();
      renderList(metricConfig(noteTarget.metric));
      renderSkillRankingPage();
      updateExport();
    }}

    function resetActiveSpell() {{
      if (!noteTarget) return;
      const c = champion(noteTarget.alias);
      const rows = adjustedAbilityRows(c, noteTarget.metric);
      const active = rows[activeSpellIndex];
      if (!active) return;
      setSpellAdjustment(noteTarget.metric, noteTarget.alias, active.spell, 0);
      renderSpellReview();
      renderList(metricConfig(noteTarget.metric));
      renderSkillRankingPage();
      updateExport();
    }}

    function closeNoteDialog() {{
      document.getElementById("noteDialog").close();
      noteTarget = null;
    }}

    function saveNoteDialog() {{
      if (!noteTarget) return;
      setNote(noteTarget.metric, noteTarget.alias, document.getElementById("noteText").value);
      closeNoteDialog();
      render();
    }}

    function deleteNoteDialog() {{
      if (!noteTarget) return;
      setNote(noteTarget.metric, noteTarget.alias, "");
      closeNoteDialog();
      render();
    }}

    function exportPayload() {{
      const out = {{
        type: "semantic_score_manual_ranking",
        generated_at: new Date().toISOString(),
        source_data_generated_at: DATA.generated_at,
        data_dragon_version: DATA.data_dragon_version,
        rankings: {{}},
        ranking_details: {{}},
        notes: {{}},
        changes: {{}},
      }};
      for (const metric of METRICS) {{
        const order = scoreOrder(metric.key);
        const modelRanks = rankMap(modelOrder[metric.key]);
        out.rankings[metric.key] = order;
        const rows = order.map((alias, idx) => {{
          const c = champion(alias);
          const manualRank = idx + 1;
          const modelRank = modelRanks.get(alias);
          const note = getNote(metric.key, alias);
          const adjustedRows = adjustedAbilityRows(c, metric.key);
          const topSpells = adjustedRows.map(row => row.spell).join(",");
          const spellScores = adjustedRows.map(row => {{
            const adjustment = row.adjustment || 0;
            return {{
              spell: row.spell,
              base_score: row.baseScore,
              manual_adjustment: adjustment,
              adjusted_score: Number(row.adjustedScore.toFixed(2)),
              detail: [row.shape, row.condition].filter(Boolean).join(" / "),
            }};
          }});
          const hasSpellAdjustment = spellScores.some(row => row.manual_adjustment);
          const scoreAdjustment = metricScoreAdjustment(c, metric.key);
          return {{
            champion_alias: alias,
            champion_name_en: c.name_en,
            champion_name_zh: c.name_zh,
            score: c[metric.key],
            score_adjustment: Number(scoreAdjustment.toFixed(2)),
            adjusted_score: Number((c[metric.key] + scoreAdjustment).toFixed(2)),
            model_rank: modelRank,
            manual_rank: manualRank,
            delta_rank: modelRank - manualRank,
            top_spells: topSpells,
            top_spells_display: formatSpellChain(topSpells),
            spell_scores: spellScores,
            note,
            has_spell_adjustment: hasSpellAdjustment,
          }};
        }});
        out.ranking_details[metric.key] = rows;
        out.notes[metric.key] = Object.fromEntries(
          rows.filter(row => row.note).map(row => [row.champion_alias, row.note])
        );
        out.spell_adjustments = out.spell_adjustments || {{}};
        out.spell_adjustments[metric.key] = Object.fromEntries(
          rows
            .filter(row => row.has_spell_adjustment)
            .map(row => [
              row.champion_alias,
              Object.fromEntries(
                row.spell_scores
                  .filter(score => score.manual_adjustment)
                  .map(score => [score.spell, score.manual_adjustment])
              ),
            ])
        );
        out.changes[metric.key] = rows.filter(row => row.delta_rank !== 0 || row.note || row.has_spell_adjustment);
      }}
      return out;
    }}

    function updateExport() {{
      document.getElementById("exportBox").value = JSON.stringify(exportPayload(), null, 2);
    }}

    function downloadExport() {{
      const payload = exportPayload();
      const text = JSON.stringify(payload, null, 2);
      document.getElementById("exportBox").value = text;
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      const blob = new Blob([text], {{ type: "application/json;charset=utf-8" }});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `semantic-score-review-${{stamp}}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }}

    function skillRankingRows(metricKey, categoryKey = "") {{
      const scoreKey = metricKey === "wave" ? "wave_score" : "engage_score";
      return (DATA.skills || [])
        .map(row => {{
          const baseScore = Number(row[scoreKey] || 0);
          const adjustment = getSpellAdjustment(metricKey, row.champion_alias, row.spell);
          return {{
            ...row,
            baseScore,
            adjustment,
            adjustedScore: clampScoreFloor(baseScore + adjustment),
          }};
        }})
        .filter(row => row.adjustedScore > 0)
        .filter(row => !categoryKey || skillCategory(row) === categoryKey)
        .filter(row => {{
          const c = champion(row.champion_alias);
          if (!c || !matchesRole(c)) return false;
          const query = document.getElementById("search").value.trim().toLowerCase();
          const haystack = `${{row.champion_alias}} ${{row.champion_name_en}} ${{row.champion_name_zh}} ${{row.spell}} ${{row.spell_name_en}} ${{c.tags.join(" ")}}`.toLowerCase();
          return !query || haystack.includes(query);
        }})
        .sort((a, b) =>
          b.adjustedScore - a.adjustedScore ||
          a.champion_name_en.localeCompare(b.champion_name_en) ||
          a.spell.localeCompare(b.spell)
        );
    }}

    function isGroupSkill(row) {{
      return row.cc_type === "aoe_hard" ||
        row.shape === "circle" ||
        row.shape === "cone" ||
        Number(row.expected_targets || 0) >= 1.35;
    }}

    function skillCategory(row) {{
      const ccType = row.cc_type || "none";
      const gate = row.engage_gate || "none";
      if (ccType === "soft_slow") return isGroupSkill(row) ? "aoe_slow" : "single_slow";
      if (gate === "forced_displacement" || ccType === "hook_pull") return "single_control";
      if (ccType !== "none") return isGroupSkill(row) ? "aoe_control" : "single_control";
      if (gate === "mobility_only" || row.shape === "dash") return "mobility";
      if (Number(row.damage_component || 0) >= 0.2) return "damage";
      return "utility";
    }}

    function skillRankRowHtml(row, idx) {{
      const c = champion(row.champion_alias) || {{}};
      const imageUrl = row.image_url || c.image_url || "";
      const detail = [row.cc_type, row.engage_gate].filter(Boolean).join(" / ") ||
        [row.shape, row.condition].filter(Boolean).join(" / ");
      const scoreClass = row.adjustment < 0 ? "negative" : "";
      return `
        <div class="skill-rank-row">
          <strong>${{String(idx + 1).padStart(2, "0")}}</strong>
          ${{imageUrl ? `<img class="skill-rank-icon" src="${{imageUrl}}" alt="" loading="lazy" />` : `<span></span>`}}
          <span title="${{row.champion_name_en}} ${{row.spell}}">${{row.champion_name_en}} ${{row.spell}}</span>
          <span title="${{row.spell_name_en}}">${{row.spell_name_en || ""}}<br><small>${{detail || "none"}}</small></span>
          <span class="score ${{row.adjustment ? "manual" : ""}}">
            ${{formatScore(row.adjustedScore)}}${{row.adjustment ? `<span class="score-adjust ${{scoreClass}}">${{signedScore(row.adjustment)}}</span>` : ""}}
          </span>
          <span class="skill-rank-actions">
            <button type="button" data-skill-adjust="-1" data-alias="${{row.champion_alias}}" data-spell="${{row.spell}}" title="Decrease score">−</button>
            <button type="button" data-skill-adjust="0" data-alias="${{row.champion_alias}}" data-spell="${{row.spell}}" title="Reset score">0</button>
            <button type="button" data-skill-adjust="1" data-alias="${{row.champion_alias}}" data-spell="${{row.spell}}" title="Increase score">+</button>
          </span>
        </div>
      `;
    }}

    function adjustRankedSkill(alias, spell, direction, event) {{
      const current = getSpellAdjustment("engage", alias, spell);
      const next = direction === 0 ? 0 : current + spellStep(direction, event);
      setSpellAdjustment("engage", alias, spell, next);
      renderSkillRankingPage();
      renderList(metricConfig("engage"));
      if (noteTarget && noteTarget.metric === "engage" && noteTarget.alias === alias) {{
        renderSpellReview();
      }}
      updateExport();
    }}

    function renderSkillRankingPage() {{
      const board = document.getElementById("skillRankingBoard");
      if (!board) return;
      board.innerHTML = SKILL_RANK_CATEGORIES.map(category => {{
        const rows = skillRankingRows("engage", category.key).slice(0, 50);
        return `
          <section class="skill-category">
            <div class="skill-category-head">
              <strong>${{category.label}}</strong>
              <span>${{category.hint}}</span>
            </div>
            <div class="skill-category-list">
              ${{rows.map(skillRankRowHtml).join("") || `<div class="empty-state">No matching skills.</div>`}}
            </div>
          </section>
        `;
      }}).join("");
      board.querySelectorAll("[data-skill-adjust]").forEach(button => {{
        button.addEventListener("click", event => {{
          event.stopPropagation();
          const direction = Number(button.dataset.skillAdjust || 0);
          adjustRankedSkill(button.dataset.alias || "", button.dataset.spell || "", direction, event);
        }});
      }});
    }}

    function spellStep(sign, event) {{
      const magnitude = event && event.shiftKey ? 0.01 : event && event.altKey ? 0.1 : 0.05;
      return sign * magnitude;
    }}

    function bindRepeatButton(id, action) {{
      const button = document.getElementById(id);
      let delayTimer = null;
      let repeatTimer = null;
      let suppressClick = false;

      const stop = () => {{
        if (delayTimer) clearTimeout(delayTimer);
        if (repeatTimer) clearInterval(repeatTimer);
        delayTimer = null;
        repeatTimer = null;
      }};

      button.addEventListener("pointerdown", event => {{
        if (event.button !== 0) return;
        event.preventDefault();
        suppressClick = true;
        if (button.setPointerCapture) button.setPointerCapture(event.pointerId);
        action(event);
        stop();
        delayTimer = setTimeout(() => {{
          repeatTimer = setInterval(() => action(event), 82);
        }}, 260);
      }});
      button.addEventListener("click", event => {{
        if (suppressClick) {{
          suppressClick = false;
          return;
        }}
        action(event);
      }});
      ["pointerup", "pointercancel", "lostpointercapture", "pointerleave", "blur"].forEach(type => {{
        button.addEventListener(type, stop);
      }});
    }}

    document.getElementById("search").addEventListener("input", render);
    document.getElementById("exportBtn").addEventListener("click", downloadExport);
    document.getElementById("resetBtn").addEventListener("click", () => {{
      localStorage.removeItem(stateKey);
      state.wave = null;
      state.engage = null;
      state.notes = {{ wave: {{}}, engage: {{}} }};
      state.spellAdjustments = {{ wave: {{}}, engage: {{}} }};
      render();
    }});
    document.getElementById("noteSaveBtn").addEventListener("click", saveNoteDialog);
    document.getElementById("noteDeleteBtn").addEventListener("click", deleteNoteDialog);
    document.getElementById("noteCancelBtn").addEventListener("click", closeNoteDialog);
    bindRepeatButton("spellUpBtn", event => adjustActiveSpell(spellStep(1, event)));
    bindRepeatButton("spellDownBtn", event => adjustActiveSpell(spellStep(-1, event)));
    document.getElementById("spellResetBtn").addEventListener("click", resetActiveSpell);
    document.getElementById("noteText").addEventListener("keydown", event => {{
      if (event.key === "Enter" && !event.shiftKey) {{
        event.preventDefault();
        saveNoteDialog();
      }}
    }});
    document.getElementById("noteDialog").addEventListener("keydown", event => {{
      if (!noteTarget) return;
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {{
        event.preventDefault();
        adjustActiveSpell(spellStep(event.key === "ArrowUp" ? 1 : -1, event));
      }}
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {{
        event.preventDefault();
        const c = champion(noteTarget.alias);
        const rows = parseAbilityBreakdown(c, noteTarget.metric);
        const direction = event.key === "ArrowRight" ? 1 : -1;
        activeSpellIndex = Math.max(0, Math.min(rows.length - 1, activeSpellIndex + direction));
        renderSpellReview();
      }}
    }});
    document.getElementById("noteDialog").addEventListener("cancel", () => {{
      noteTarget = null;
    }});
    document.querySelectorAll(".tab").forEach(tab => {{
      tab.addEventListener("click", () => {{
        activeMetric = tab.dataset.tab;
        localStorage.setItem("semantic-score-review:active-tab", activeMetric);
        render();
      }});
    }});
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerCancel);
    document.addEventListener("wheel", onWheelDuringDrag, {{ passive: false }});

    render();
  </script>
</body>
</html>
"""


def write_role_definitions_json(out: Path, payload: dict[str, Any]) -> Path:
    role_spec = role_definitions_payload()
    role_spec["generated_at"] = payload.get("generated_at")
    role_spec["data_dragon_version"] = payload.get("data_dragon_version")
    role_spec["current_roles"] = dict(
        sorted(
            (
                row["alias"],
                {
                    "primary": (row.get("tags") or [""])[0],
                    "secondary": "",
                    "tags": row.get("tags") or [],
                },
            )
            for row in payload.get("champions", [])
            if row.get("alias")
        )
    )
    role_path = out.parent / "champion-roles.json"
    role_path.write_text(json.dumps(role_spec, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return role_path


@click.command()
@click.option(
    "--score-csv",
    type=click.Path(exists=True, path_type=Path),
    default=DEFAULT_SCORE_CSV,
    show_default=True,
)
@click.option(
    "--ability-json",
    type=click.Path(exists=True, path_type=Path),
    default=DEFAULT_ABILITY_JSON,
    show_default=True,
)
@click.option(
    "--skill-debug-csv",
    type=click.Path(exists=True, path_type=Path),
    default=DEFAULT_SKILL_DEBUG_CSV,
    show_default=True,
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=DEFAULT_OUT,
    show_default=True,
)
def main(score_csv: Path, ability_json: Path, skill_debug_csv: Path, out: Path) -> None:
    payload = load_scores(score_csv, ability_json, skill_debug_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page_html(payload), encoding="utf-8")
    role_path = write_role_definitions_json(out, payload)
    click.echo(f"[semantic-review] wrote {out}")
    click.echo(f"[semantic-review] wrote {role_path}")
    click.echo(f"[semantic-review] champions: {len(payload['champions'])}")


if __name__ == "__main__":
    main()
