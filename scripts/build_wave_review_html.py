"""Build a draggable HTML review board for wave-clear score calibration."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCORE_CSV = ROOT / "data" / "cache" / "champion_semantic_scores.csv"
ABILITY_JSON = ROOT / "data" / "cache" / "champion_abilities.json"
REVIEW_CSV = ROOT / "documents" / "reports" / "wave_score_bucket_review_2026_05_26.csv"
OUT_HTML = ROOT / "documents" / "reports" / "wave_score_bucket_review_2026_05_26.html"


BUCKETS = [
    {"id": "S", "title": "S", "desc": "頂級清線核心", "range": "1.65+"},
    {"id": "A", "title": "A", "desc": "強清線", "range": "1.35-1.64"},
    {"id": "B", "title": "B", "desc": "穩定清線", "range": "1.05-1.34"},
    {"id": "C", "title": "C", "desc": "普通或條件清線", "range": "0.75-1.04"},
    {"id": "D", "title": "D", "desc": "弱清線", "range": "0.45-0.74"},
    {"id": "F", "title": "F", "desc": "幾乎沒有清線", "range": "<0.45"},
]

FIELDNAMES = [
    "current_bucket",
    "corrected_bucket",
    "corrected_score",
    "champion_alias",
    "champion_name_zh",
    "tags",
    "wave_clear_score",
    "basic_attack_score",
    "poke_score",
    "damage_score",
    "engage_score",
    "wave_top_spells",
    "current_wave_evidence",
    "review_flag",
    "review_note",
]


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Wave Score Bucket Review</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1217;
      --panel: #171c23;
      --panel-2: #222936;
      --panel-3: #11161d;
      --line: #303844;
      --line-soft: #252d38;
      --text: #e8eaed;
      --muted: #a0a7b1;
      --dim: #737d8b;
      --accent: #83d7ff;
      --good: #9ee06f;
      --warn: #f3c567;
      --bad: #ff7b72;
      --focus: #cfb3ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif;
      font-size: 13px;
      line-height: 1.45;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 30;
      border-bottom: 1px solid var(--line);
      background: rgba(15, 18, 23, .95);
      backdrop-filter: blur(14px);
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(270px, 1fr) auto;
      gap: 16px;
      align-items: center;
      padding: 14px 18px 12px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 750;
      letter-spacing: 0;
    }
    .meta {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      align-items: center;
    }
    .search {
      width: min(360px, 34vw);
      min-width: 220px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-3);
      color: var(--text);
      padding: 8px 10px;
      outline: none;
    }
    button, select, input, textarea {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      padding: 8px 10px;
      cursor: pointer;
    }
    button:hover {
      border-color: #596473;
      background: #27303d;
    }
    .primary {
      border-color: rgba(131, 215, 255, .5);
      color: var(--accent);
    }
    .toggle-active {
      border-color: rgba(158, 224, 111, .58);
      color: var(--good);
    }
    .search:focus,
    select:focus,
    input:focus,
    textarea:focus,
    button:focus-visible {
      border-color: var(--focus);
      box-shadow: 0 0 0 2px rgba(207, 179, 255, .18);
      outline: none;
    }
    main { overflow-x: auto; }
    .board {
      display: grid;
      grid-template-columns: repeat(6, minmax(262px, 1fr));
      gap: 10px;
      min-width: 1572px;
      padding: 12px;
    }
    .bucket {
      display: flex;
      flex-direction: column;
      min-height: calc(100vh - 96px);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .bucket-head {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 8px;
      align-items: center;
      padding: 10px;
      border-bottom: 1px solid var(--line);
    }
    .bucket-title {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 30px;
      height: 30px;
      border-radius: 6px;
      background: var(--panel-3);
      font-size: 16px;
      font-weight: 850;
    }
    .bucket[data-bucket="S"] .bucket-title { color: #f0d77b; }
    .bucket[data-bucket="A"] .bucket-title { color: #9ee06f; }
    .bucket[data-bucket="B"] .bucket-title { color: #83d7ff; }
    .bucket[data-bucket="C"] .bucket-title { color: #cfb3ff; }
    .bucket[data-bucket="D"] .bucket-title { color: #f3c567; }
    .bucket[data-bucket="F"] .bucket-title { color: #ff7b72; }
    .bucket-copy { min-width: 0; }
    .bucket-copy strong {
      display: block;
      font-size: 12px;
      font-weight: 700;
    }
    .bucket-copy span {
      display: block;
      color: var(--muted);
      font-size: 11px;
    }
    .count {
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
    .dropzone {
      display: flex;
      flex-direction: column;
      gap: 8px;
      flex: 1;
      min-height: 100%;
      padding: 8px;
    }
    .bucket.drag-over {
      border-color: rgba(131, 215, 255, .7);
      box-shadow: inset 0 0 0 1px rgba(131, 215, 255, .28);
    }
    .card {
      border: 1px solid var(--line-soft);
      border-radius: 7px;
      background: var(--panel-3);
      padding: 8px;
      cursor: grab;
      box-shadow: 0 1px 0 rgba(255, 255, 255, .025);
    }
    .card:hover { border-color: #414c5c; }
    .card.dragging {
      opacity: .48;
      transform: scale(.99);
    }
    .card.changed { border-color: rgba(158, 224, 111, .45); }
    .card.flagged { box-shadow: inset 0 0 0 1px rgba(243, 197, 103, .24); }
    .card-top {
      display: grid;
      grid-template-columns: 38px 1fr auto;
      gap: 8px;
      align-items: center;
    }
    .icon {
      width: 38px;
      height: 38px;
      border: 1px solid #303845;
      border-radius: 5px;
      background: #222936;
      object-fit: cover;
    }
    .name { min-width: 0; }
    .name strong {
      display: block;
      overflow: hidden;
      font-size: 14px;
      font-weight: 750;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .name span {
      display: block;
      overflow: hidden;
      color: var(--muted);
      font-size: 11px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .score {
      color: var(--accent);
      font-weight: 850;
      font-variant-numeric: tabular-nums;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 4px;
      margin-top: 8px;
    }
    .metric {
      min-width: 0;
      border: 1px solid var(--line-soft);
      border-radius: 5px;
      color: var(--muted);
      padding: 4px 5px;
      font-size: 11px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    .metric b {
      float: right;
      color: var(--text);
      font-weight: 700;
    }
    .evidence, .flag {
      margin-top: 7px;
      border-radius: 5px;
      padding: 6px 7px;
      font-size: 11px;
    }
    .evidence {
      border: 1px solid var(--line-soft);
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .flag {
      border: 1px solid rgba(243, 197, 103, .25);
      background: rgba(243, 197, 103, .08);
      color: #f2d18c;
    }
    .edit {
      display: grid;
      grid-template-columns: 54px 66px 1fr;
      gap: 6px;
      margin-top: 8px;
    }
    select, input, textarea {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: #0f141b;
      color: var(--text);
      padding: 6px;
    }
    textarea {
      height: 32px;
      resize: vertical;
    }
    .empty {
      display: none;
      margin: 0 8px 8px;
      border: 1px dashed var(--line);
      border-radius: 7px;
      color: var(--dim);
      padding: 16px 8px;
      text-align: center;
    }
    .dropzone:empty + .empty { display: block; }
    .toast {
      position: fixed;
      right: 16px;
      bottom: 16px;
      z-index: 50;
      border: 1px solid rgba(158, 224, 111, .35);
      border-radius: 7px;
      background: #111a13;
      color: var(--good);
      padding: 10px 12px;
      opacity: 0;
      pointer-events: none;
      transform: translateY(8px);
      transition: opacity .16s ease-out, transform .16s ease-out;
    }
    .toast.show {
      opacity: 1;
      transform: translateY(0);
    }
    @media (max-width: 780px) {
      .topbar { grid-template-columns: 1fr; }
      .toolbar { justify-content: stretch; }
      .search {
        width: 100%;
        min-width: 0;
      }
      button { flex: 1; }
      .board { grid-template-columns: repeat(6, 262px); }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Wave Score Bucket Review</h1>
        <div class="meta">拖曳英雄調整清兵 tier，匯出後可轉成 wave score override。Data Dragon __VERSION__，__COUNT__ champions</div>
      </div>
      <div class="toolbar">
        <input id="search" class="search" type="search" placeholder="搜尋英雄 / alias / evidence" autocomplete="off" />
        <button id="flagToggle" type="button">只看可疑</button>
        <button id="changedToggle" type="button">只看已改</button>
        <button id="exportJson" class="primary" type="button">Export JSON</button>
        <button id="exportCsv" class="primary" type="button">Export CSV</button>
        <button id="reset" type="button">Reset</button>
      </div>
    </div>
  </header>
  <main>
    <section id="board" class="board" aria-label="Wave buckets"></section>
  </main>
  <div id="toast" class="toast">Saved</div>
  <script id="payload" type="application/json">__DATA_JSON__</script>
  <script>
    const DATA = JSON.parse(document.getElementById("payload").textContent);
    const STORAGE_KEY = "wave-score-bucket-review:2026-05-26:v1";
    const buckets = DATA.buckets;
    const original = new Map(DATA.rows.map(row => [row.champion_alias, row]));
    let state = new Map(DATA.rows.map(row => [row.champion_alias, {
      bucket: row.corrected_bucket || row.current_bucket,
      corrected_score: row.corrected_score || "",
      review_note: row.review_note || "",
    }]));
    let filters = { search: "", flaggedOnly: false, changedOnly: false };
    let draggedAlias = null;

    function loadState() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return;
        const saved = JSON.parse(raw);
        Object.entries(saved).forEach(([alias, value]) => {
          if (original.has(alias)) state.set(alias, { ...state.get(alias), ...value });
        });
      } catch {
      }
    }

    function saveState(show = false) {
      const obj = {};
      state.forEach((value, alias) => { obj[alias] = value; });
      localStorage.setItem(STORAGE_KEY, JSON.stringify(obj));
      if (show) toast("Saved locally");
    }

    function isChanged(alias) {
      const row = original.get(alias);
      const s = state.get(alias);
      return s.bucket !== row.current_bucket || !!s.corrected_score || !!s.review_note;
    }

    function visible(row) {
      const q = filters.search.trim().toLowerCase();
      if (filters.flaggedOnly && !row.review_flag) return false;
      if (filters.changedOnly && !isChanged(row.champion_alias)) return false;
      if (!q) return true;
      return [row.champion_alias, row.champion_name_zh, row.tags, row.review_flag, row.current_wave_evidence, row.wave_top_spells]
        .some(value => String(value || "").toLowerCase().includes(q));
    }

    function card(row) {
      const s = state.get(row.champion_alias);
      const el = document.createElement("article");
      el.className = "card";
      if (row.review_flag) el.classList.add("flagged");
      if (isChanged(row.champion_alias)) el.classList.add("changed");
      el.draggable = true;
      el.dataset.alias = row.champion_alias;
      el.innerHTML = `
        <div class="card-top">
          <img class="icon" src="${escapeAttr(row.image_url)}" alt="" loading="lazy">
          <div class="name">
            <strong>${escapeHtml(row.champion_name_zh || row.champion_alias)} / ${escapeHtml(row.champion_alias)}</strong>
            <span>${escapeHtml(row.tags || "")}</span>
          </div>
          <div class="score">${escapeHtml(row.wave_clear_score)}</div>
        </div>
        <div class="metrics">
          <div class="metric">AA <b>${escapeHtml(row.basic_attack_score)}</b></div>
          <div class="metric">poke <b>${escapeHtml(row.poke_score)}</b></div>
          <div class="metric">dmg <b>${escapeHtml(row.damage_score)}</b></div>
        </div>
        ${row.current_wave_evidence ? `<div class="evidence">${escapeHtml(row.current_wave_evidence)}</div>` : ""}
        ${row.review_flag ? `<div class="flag">${escapeHtml(row.review_flag)}</div>` : ""}
        <div class="edit">
          <select aria-label="corrected bucket">
            ${buckets.map(b => `<option value="${b.id}" ${s.bucket === b.id ? "selected" : ""}>${b.id}</option>`).join("")}
          </select>
          <input aria-label="corrected score" type="number" min="0" max="3" step="0.05" placeholder="score" value="${escapeAttr(s.corrected_score)}">
          <textarea aria-label="review note" placeholder="備註">${escapeHtml(s.review_note)}</textarea>
        </div>
      `;
      el.addEventListener("dragstart", event => {
        draggedAlias = row.champion_alias;
        el.classList.add("dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", row.champion_alias);
      });
      el.addEventListener("dragend", () => {
        draggedAlias = null;
        el.classList.remove("dragging");
        document.querySelectorAll(".bucket").forEach(bucket => bucket.classList.remove("drag-over"));
      });
      const [select, input, textarea] = el.querySelectorAll("select,input,textarea");
      select.addEventListener("change", () => {
        updateRow(row.champion_alias, { bucket: select.value }, true);
        render();
      });
      input.addEventListener("input", () => updateRow(row.champion_alias, { corrected_score: input.value }, false));
      input.addEventListener("change", () => {
        saveState(true);
        renderCounts();
      });
      textarea.addEventListener("input", () => updateRow(row.champion_alias, { review_note: textarea.value }, false));
      textarea.addEventListener("change", () => {
        saveState(true);
        renderCounts();
      });
      return el;
    }

    function updateRow(alias, patch, persist) {
      state.set(alias, { ...state.get(alias), ...patch });
      if (persist) saveState(true);
    }

    function render() {
      const board = document.getElementById("board");
      board.innerHTML = "";
      buckets.forEach(bucket => {
        const section = document.createElement("section");
        section.className = "bucket";
        section.dataset.bucket = bucket.id;
        section.innerHTML = `
          <div class="bucket-head">
            <div class="bucket-title">${bucket.title}</div>
            <div class="bucket-copy"><strong>${escapeHtml(bucket.desc)}</strong><span>${escapeHtml(bucket.range)}</span></div>
            <div class="count" data-count>0</div>
          </div>
          <div class="dropzone" data-dropzone></div>
          <div class="empty">沒有卡片</div>
        `;
        attachDrop(section);
        board.appendChild(section);
      });
      DATA.rows
        .filter(visible)
        .sort((a, b) => Number(b.wave_clear_score) - Number(a.wave_clear_score) || a.champion_alias.localeCompare(b.champion_alias))
        .forEach(row => {
          const bucket = state.get(row.champion_alias).bucket || row.current_bucket;
          const target = document.querySelector(`.bucket[data-bucket="${bucket}"] [data-dropzone]`);
          if (target) target.appendChild(card(row));
        });
      renderCounts();
    }

    function attachDrop(section) {
      section.addEventListener("dragover", event => {
        event.preventDefault();
        section.classList.add("drag-over");
      });
      section.addEventListener("dragleave", event => {
        if (!section.contains(event.relatedTarget)) section.classList.remove("drag-over");
      });
      section.addEventListener("drop", event => {
        event.preventDefault();
        const alias = event.dataTransfer.getData("text/plain") || draggedAlias;
        if (!alias || !state.has(alias)) return;
        updateRow(alias, { bucket: section.dataset.bucket }, true);
        render();
      });
    }

    function renderCounts() {
      document.querySelectorAll(".bucket").forEach(bucket => {
        bucket.querySelector("[data-count]").textContent = bucket.querySelectorAll(".card").length;
      });
      document.getElementById("changedToggle").classList.toggle("toggle-active", filters.changedOnly);
      document.getElementById("flagToggle").classList.toggle("toggle-active", filters.flaggedOnly);
    }

    function exportRows() {
      return DATA.rows.map(row => {
        const s = state.get(row.champion_alias);
        return {
          champion_alias: row.champion_alias,
          champion_name_zh: row.champion_name_zh,
          tags: row.tags,
          current_bucket: row.current_bucket,
          current_wave_score: row.wave_clear_score,
          corrected_bucket: s.bucket === row.current_bucket ? "" : s.bucket,
          corrected_score: s.corrected_score,
          review_note: s.review_note,
          review_flag: row.review_flag,
          wave_top_spells: row.wave_top_spells,
          current_wave_evidence: row.current_wave_evidence,
        };
      });
    }

    function download(filename, type, text) {
      const blob = new Blob([text], { type });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast(`Exported ${filename}`);
    }

    function toCsv(rows) {
      const headers = Object.keys(rows[0]);
      const line = values => values.map(value => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",");
      return [line(headers), ...rows.map(row => line(headers.map(h => row[h])))].join("\n");
    }

    function toast(message) {
      const t = document.getElementById("toast");
      t.textContent = message;
      t.classList.add("show");
      clearTimeout(toast.timer);
      toast.timer = setTimeout(() => t.classList.remove("show"), 1400);
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>'"]/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "'": "&#39;",
        '"': "&quot;",
      }[ch]));
    }

    function escapeAttr(value) {
      return escapeHtml(value);
    }

    document.getElementById("search").addEventListener("input", event => {
      filters.search = event.target.value;
      render();
    });
    document.getElementById("flagToggle").addEventListener("click", () => {
      filters.flaggedOnly = !filters.flaggedOnly;
      render();
    });
    document.getElementById("changedToggle").addEventListener("click", () => {
      filters.changedOnly = !filters.changedOnly;
      render();
    });
    document.getElementById("exportJson").addEventListener("click", () => {
      download("wave-score-review-corrections-2026-05-26.json", "application/json;charset=utf-8", JSON.stringify(exportRows(), null, 2));
    });
    document.getElementById("exportCsv").addEventListener("click", () => {
      download("wave-score-review-corrections-2026-05-26.csv", "text/csv;charset=utf-8", toCsv(exportRows()));
    });
    document.getElementById("reset").addEventListener("click", () => {
      if (!confirm("清掉本機儲存的 wave 校正結果？")) return;
      localStorage.removeItem(STORAGE_KEY);
      state = new Map(DATA.rows.map(row => [row.champion_alias, { bucket: row.current_bucket, corrected_score: "", review_note: "" }]));
      render();
      toast("Reset");
    });

    loadState();
    render();
  </script>
</body>
</html>
"""


def bucket_for(score: float) -> str:
    if score >= 1.65:
        return "S"
    if score >= 1.35:
        return "A"
    if score >= 1.05:
        return "B"
    if score >= 0.75:
        return "C"
    if score >= 0.45:
        return "D"
    return "F"


def score_text(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    text = f"{num:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def note_section(notes: str, prefix: str) -> str:
    for part in str(notes or "").split("; "):
        if part.startswith(prefix):
            return part
    return ""


def review_flag(row: dict[str, str]) -> str:
    try:
        wave = float(row.get("wave_clear_score") or 0.0)
        basic_attack = float(row.get("basic_attack_score") or 0.0)
        damage = float(row.get("damage_score") or 0.0)
    except ValueError:
        return ""
    tags = set(str(row.get("tags") or "").split("|"))
    if basic_attack >= wave and basic_attack >= 0.75:
        return "AA floor 主導，確認普攻是否真的能穩定清線"
    if wave < 0.75 and damage >= 2.8 and ({"Mage", "Marksman"} & tags):
        return "高傷害但低清線，確認是否漏算 AoE 或彈射"
    if wave >= 1.35 and "wave_clear=" not in str(row.get("notes") or ""):
        return "高分但缺少技能 evidence，建議檢查"
    return ""


def load_existing_review() -> dict[str, dict[str, str]]:
    if not REVIEW_CSV.exists():
        return {}
    with REVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("champion_alias") or ""): row
            for row in csv.DictReader(handle)
            if row.get("champion_alias")
        }


def build_rows(version: str) -> list[dict[str, str]]:
    existing = load_existing_review()
    rows: list[dict[str, str]] = []
    with SCORE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            alias = str(source.get("champion_alias") or "")
            score = float(source.get("wave_clear_score") or 0.0)
            prior = existing.get(alias, {})
            row = {
                "current_bucket": bucket_for(score),
                "corrected_bucket": prior.get("corrected_bucket", ""),
                "corrected_score": prior.get("corrected_score", ""),
                "champion_alias": alias,
                "champion_name_zh": str(source.get("champion_name_zh") or ""),
                "tags": str(source.get("tags") or ""),
                "wave_clear_score": score_text(source.get("wave_clear_score")),
                "basic_attack_score": score_text(source.get("basic_attack_score") or source.get("st_floor")),
                "poke_score": score_text(source.get("poke_score")),
                "damage_score": score_text(source.get("damage_score")),
                "engage_score": score_text(source.get("engage_score")),
                "wave_top_spells": str(source.get("wave_top_spells") or ""),
                "current_wave_evidence": note_section(str(source.get("notes") or ""), "wave_clear="),
                "review_flag": "",
                "review_note": prior.get("review_note", ""),
                "image_url": f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{alias}.png",
                "notes": str(source.get("notes") or ""),
            }
            row["review_flag"] = prior.get("review_flag") or review_flag(row)
            rows.append(row)
    rows.sort(key=lambda row: (-float(row["wave_clear_score"]), row["champion_alias"]))
    return rows


def write_review_csv(rows: list[dict[str, str]]) -> None:
    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDNAMES})


def main() -> None:
    raw = json.loads(ABILITY_JSON.read_text(encoding="utf-8"))
    version = str(raw.get("version") or "16.10.1")
    rows = build_rows(version)
    write_review_csv(rows)
    payload = {
        "generatedFrom": str(REVIEW_CSV.relative_to(ROOT)).replace("\\", "/"),
        "dataDragonVersion": version,
        "buckets": BUCKETS,
        "rows": [{key: value for key, value in row.items() if key != "notes"} for row in rows],
    }
    data_json = html.escape(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        quote=False,
    )
    rendered = (
        HTML_TEMPLATE
        .replace("__DATA_JSON__", data_json)
        .replace("__VERSION__", html.escape(version))
        .replace("__COUNT__", str(len(rows)))
    )
    OUT_HTML.write_text(rendered, encoding="utf-8")
    print(REVIEW_CSV.relative_to(ROOT))
    print(OUT_HTML.relative_to(ROOT))
    print(f"embedded_rows={len(rows)}")


if __name__ == "__main__":
    main()
