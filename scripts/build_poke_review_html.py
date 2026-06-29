"""Build a draggable HTML review board for poke-score bucket calibration."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "documents" / "poke_score_bucket_review_2026_05_25.csv"
ABILITY_JSON = ROOT / "data" / "cache" / "champion_abilities.json"
OUT_PATH = ROOT / "documents" / "poke_score_bucket_review_2026_05_25.html"


BUCKETS = [
    {"id": "S", "title": "S", "desc": "頂級 poke 核心", "range": "2.4+"},
    {"id": "A", "title": "A", "desc": "穩定 poke", "range": "1.8-2.39"},
    {"id": "B", "title": "B", "desc": "有 poke 能力", "range": "1.2-1.79"},
    {"id": "C", "title": "C", "desc": "零散 poke / 半套", "range": "0.6-1.19"},
    {"id": "D", "title": "D", "desc": "很弱 poke", "range": "0.01-0.59"},
    {"id": "F", "title": "F", "desc": "幾乎沒有 poke", "range": "0"},
]


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Poke Score Bucket Review</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0e1116;
      --panel: #161a22;
      --panel-2: #1f2530;
      --panel-3: #11151d;
      --line: #30363d;
      --line-soft: #252b35;
      --text: #e6e8eb;
      --muted: #9aa0a6;
      --dim: #6f7784;
      --accent: #8bdbff;
      --good: #9ee66f;
      --warn: #f6c760;
      --bad: #ff7b72;
      --focus: #d8b8ff;
      --radius: 8px;
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
      background: rgba(14, 17, 22, .94);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(14px);
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto;
      gap: 16px;
      align-items: center;
      padding: 14px 18px 12px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      letter-spacing: 0;
      font-weight: 700;
    }
    .meta {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
    }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
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
    .search:focus,
    select:focus,
    input:focus,
    textarea:focus,
    button:focus-visible {
      border-color: var(--focus);
      box-shadow: 0 0 0 2px rgba(216, 184, 255, .18);
      outline: none;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
    }
    button:hover {
      border-color: #58606b;
      background: #252b36;
    }
    .primary {
      border-color: rgba(139, 219, 255, .45);
      color: var(--accent);
    }
    .toggle-active {
      border-color: rgba(158, 230, 111, .55);
      color: var(--good);
    }
    main { overflow-x: auto; }
    .board {
      display: grid;
      grid-template-columns: repeat(6, minmax(260px, 1fr));
      gap: 10px;
      min-width: 1560px;
      padding: 12px;
    }
    .bucket {
      display: flex;
      flex-direction: column;
      min-height: calc(100vh - 96px);
      border: 1px solid var(--line);
      border-radius: var(--radius);
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
      font-weight: 800;
    }
    .bucket[data-bucket="S"] .bucket-title { color: #f0d77b; }
    .bucket[data-bucket="A"] .bucket-title { color: #9ee66f; }
    .bucket[data-bucket="B"] .bucket-title { color: #8bdbff; }
    .bucket[data-bucket="C"] .bucket-title { color: #d8b8ff; }
    .bucket[data-bucket="D"] .bucket-title { color: #f6c760; }
    .bucket[data-bucket="F"] .bucket-title { color: #ff7b72; }
    .bucket-copy { min-width: 0; }
    .bucket-copy strong {
      display: block;
      font-size: 12px;
      font-weight: 650;
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
      border-color: rgba(139, 219, 255, .7);
      box-shadow: inset 0 0 0 1px rgba(139, 219, 255, .28);
    }
    .card {
      position: relative;
      border: 1px solid var(--line-soft);
      border-radius: 7px;
      background: var(--panel-3);
      padding: 8px;
      box-shadow: 0 1px 0 rgba(255, 255, 255, .02);
      cursor: grab;
    }
    .card:hover { border-color: #414957; }
    .card.dragging {
      opacity: .48;
      transform: scale(.99);
    }
    .card.changed { border-color: rgba(158, 230, 111, .45); }
    .card.flagged { box-shadow: inset 0 0 0 1px rgba(246, 199, 96, .22); }
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
      font-weight: 700;
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
      font-weight: 800;
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
      padding: 4px 5px;
      color: var(--muted);
      font-size: 11px;
    }
    .metric b {
      color: var(--text);
      font-weight: 700;
    }
    .evidence {
      margin-top: 7px;
      color: var(--dim);
      font-size: 11px;
      overflow-wrap: anywhere;
    }
    .flag {
      margin-top: 6px;
      color: var(--warn);
      font-size: 11px;
    }
    .edit {
      display: grid;
      grid-template-columns: 76px 1fr;
      gap: 6px;
      margin-top: 8px;
    }
    select,
    input,
    textarea {
      min-width: 0;
      border: 1px solid var(--line-soft);
      border-radius: 5px;
      background: #0c1017;
      color: var(--text);
      font: inherit;
    }
    select,
    input {
      height: 28px;
      padding: 4px 6px;
    }
    textarea {
      grid-column: 1 / -1;
      min-height: 44px;
      padding: 6px;
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
      padding: 10px 12px;
      border: 1px solid rgba(158, 230, 111, .35);
      border-radius: 7px;
      background: #111a13;
      color: var(--good);
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
      .board { grid-template-columns: repeat(6, 260px); }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Poke Score Bucket Review</h1>
        <div class="meta">拖動卡片調整桶位；欄位會自動存到本機瀏覽器。Data Dragon __VERSION__ · __COUNT__ champions</div>
      </div>
      <div class="toolbar">
        <input id="search" class="search" type="search" placeholder="搜尋英雄 / alias / flag" autocomplete="off" />
        <button id="flagToggle" type="button">只看可疑</button>
        <button id="changedToggle" type="button">只看已改</button>
        <button id="exportJson" class="primary" type="button">Export JSON</button>
        <button id="exportCsv" class="primary" type="button">Export CSV</button>
        <button id="reset" type="button">Reset</button>
      </div>
    </div>
  </header>
  <main>
    <section id="board" class="board" aria-label="Poke buckets"></section>
  </main>
  <div id="toast" class="toast">Saved</div>
  <script id="payload" type="application/json">__DATA_JSON__</script>
  <script>
    const DATA = JSON.parse(document.getElementById("payload").textContent);
    const STORAGE_KEY = "poke-score-bucket-review:2026-05-25:v1";
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
      state.forEach((value, alias) => {
        obj[alias] = value;
      });
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
      return [row.champion_alias, row.champion_name_zh, row.tags, row.review_flag, row.current_poke_evidence]
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
          <div class="score">${escapeHtml(row.poke_score)}</div>
        </div>
        <div class="metrics">
          <div class="metric">wave <b>${escapeHtml(row.wave_clear_score)}</b></div>
          <div class="metric">dmg <b>${escapeHtml(row.damage_score)}</b></div>
          <div class="metric">eng <b>${escapeHtml(row.engage_score)}</b></div>
        </div>
        ${row.current_poke_evidence ? `<div class="evidence">evidence: ${escapeHtml(row.current_poke_evidence)}</div>` : ""}
        ${row.review_flag ? `<div class="flag">${escapeHtml(row.review_flag)}</div>` : ""}
        <div class="edit">
          <select aria-label="corrected bucket">
            ${buckets.map(b => `<option value="${b.id}" ${s.bucket === b.id ? "selected" : ""}>${b.id}</option>`).join("")}
          </select>
          <input aria-label="corrected score" type="number" min="0" max="3" step="0.05" placeholder="score" value="${escapeAttr(s.corrected_score)}">
          <textarea aria-label="review note" placeholder="校正理由或備註">${escapeHtml(s.review_note)}</textarea>
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
          <div class="empty">拖到這裡</div>
        `;
        attachDrop(section);
        board.appendChild(section);
      });
      DATA.rows
        .filter(visible)
        .sort((a, b) => Number(b.poke_score) - Number(a.poke_score) || a.champion_alias.localeCompare(b.champion_alias))
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
          current_poke_score: row.poke_score,
          corrected_bucket: s.bucket === row.current_bucket ? "" : s.bucket,
          corrected_score: s.corrected_score,
          review_note: s.review_note,
          review_flag: row.review_flag,
          current_poke_evidence: row.current_poke_evidence,
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
      download("poke-score-review-corrections-2026-05-25.json", "application/json;charset=utf-8", JSON.stringify(exportRows(), null, 2));
    });
    document.getElementById("exportCsv").addEventListener("click", () => {
      download("poke-score-review-corrections-2026-05-25.csv", "text/csv;charset=utf-8", toCsv(exportRows()));
    });
    document.getElementById("reset").addEventListener("click", () => {
      if (!confirm("清掉本機儲存的校正結果？")) return;
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


def main() -> None:
    raw = json.loads(ABILITY_JSON.read_text(encoding="utf-8"))
    version = str(raw.get("version") or "16.10.1")
    rows: list[dict[str, str]] = []
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            alias = row.get("champion_alias") or ""
            row["image_url"] = (
                f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{alias}.png"
            )
            rows.append(row)

    payload = {
        "generatedFrom": str(CSV_PATH.relative_to(ROOT)).replace("\\", "/"),
        "dataDragonVersion": version,
        "buckets": BUCKETS,
        "rows": rows,
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
    OUT_PATH.write_text(rendered, encoding="utf-8")
    print(OUT_PATH.relative_to(ROOT))
    print(f"embedded_rows={len(rows)}")


if __name__ == "__main__":
    main()
