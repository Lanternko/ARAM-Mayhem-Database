#!/usr/bin/env python3
"""Build a self-contained HTML tool for labeling item → role filter tags.

Usage:
  python scripts/build_item_role_annotator.py
  # → exports/item-role-annotator.html

Open the HTML in a browser, correct roles, then Export JSON.
Saved annotations also persist in localStorage.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierlist_engine import load_item_metadata  # noqa: E402
from tierlist_render import (  # noqa: E402
    build_item_role_filter_map,
    item_filter_roles_for_item,
    load_item_role_filter_label_file,
)

ROLE_ORDER = ["Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank"]
ROLE_ZH = {
    "Assassin": "刺客",
    "Fighter": "戰士",
    "Mage": "法師",
    "Marksman": "射手",
    "Support": "輔助",
    "Tank": "坦克",
}
ROLE_COLOR = {
    "Assassin": "#ef4444",
    "Fighter": "#f97316",
    "Mage": "#3b82f6",
    "Marksman": "#22c55e",
    "Support": "#ec4899",
    "Tank": "#a855f7",
}


def _load_site_items() -> tuple[str, dict[str, dict]]:
    payload_path = ROOT / "docs" / "api" / "tier-list.json"
    ddv = "16.13.1"
    lut: dict[str, dict] = {}
    if payload_path.exists():
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        ddv = str(payload.get("ddv") or ddv)
        lut = dict(payload.get("itemLut") or {})
    return ddv, lut


def collect_items() -> list[dict]:
    ddv, lut = _load_site_items()
    meta = load_item_metadata(ROOT / "data" / "cache", ddragon_version=ddv)
    auto_map = build_item_role_filter_map(ddragon_version=ddv, cache_dir=ROOT / "data" / "cache")

    # Prefer site itemLut (what users actually see). Also include any completed
    # items that have an auto role but are missing from lut (rare).
    ids: set[int] = set()
    for k in lut:
        try:
            ids.add(int(k))
        except (TypeError, ValueError):
            pass
    for k in auto_map:
        try:
            ids.add(int(k))
        except (TypeError, ValueError):
            pass

    rows: list[dict] = []
    for iid in sorted(ids):
        it = meta.get(iid) or {}
        if isinstance(it, dict) and it.get("id") is None:
            it = {**it, "id": iid}
        lut_e = lut.get(str(iid)) or {}
        zh = lut_e.get("z") or it.get("name") or it.get("name_zh") or ""
        en = lut_e.get("e") or it.get("name_en") or ""
        price = lut_e.get("p") or it.get("price_total") or 0
        cats = [str(c) for c in (it.get("categories") or [])]
        auto = list(auto_map.get(str(iid)) or item_filter_roles_for_item(it) or [])
        rows.append(
            {
                "id": iid,
                "zh": zh,
                "en": en,
                "price": int(price or 0),
                "cats": cats,
                "auto": auto,
                "inLut": str(iid) in lut,
                "icon": f"https://ddragon.leagueoflegends.com/cdn/{ddv}/img/item/{iid}.png",
            }
        )
    # Site items first, then by price desc, then id
    rows.sort(key=lambda r: (0 if r["inLut"] else 1, -r["price"], r["id"]))
    return rows


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>裝備職業標註 · arammeta</title>
<style>
  :root {
    --bg: #0e1116;
    --panel: #161b22;
    --border: #2a3140;
    --text: #e6e8eb;
    --muted: #9aa0a6;
    --accent: #f5c518;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.4;
  }
  header {
    position: sticky; top: 0; z-index: 20;
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 12px 16px 10px;
  }
  h1 { margin: 0 0 4px; font-size: 18px; font-weight: 700; }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
  .toolbar {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  }
  .toolbar input[type="search"] {
    flex: 1 1 220px; min-width: 160px;
    padding: 8px 12px; border-radius: 10px;
    border: 1px solid var(--border); background: var(--panel); color: var(--text);
    font: inherit;
  }
  select, button, label.file-btn {
    padding: 7px 12px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--panel); color: var(--text); font: inherit; cursor: pointer;
  }
  button.primary { background: var(--accent); color: #111; border-color: var(--accent); font-weight: 700; }
  button:hover, select:hover, label.file-btn:hover { border-color: #4a5568; }
  button.primary:hover { filter: brightness(1.05); }
  .stats { color: var(--muted); font-size: 12px; margin-left: auto; white-space: nowrap; }
  .legend {
    display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;
  }
  .legend span {
    font-size: 11px; padding: 2px 8px; border-radius: 999px;
    border: 1px solid var(--border); color: var(--muted);
  }
  main { padding: 12px 16px 80px; max-width: 1200px; margin: 0 auto; }
  .row {
    display: grid;
    grid-template-columns: 48px minmax(140px, 1.2fr) minmax(200px, 1fr) auto;
    gap: 12px; align-items: center;
    padding: 10px 12px; margin-bottom: 8px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  }
  .row.changed { border-color: #f5c51888; box-shadow: 0 0 0 1px #f5c51833 inset; }
  .row.unlabeled { border-color: #ef444466; }
  .row.hidden { display: none; }
  .icon { width: 48px; height: 48px; border-radius: 8px; background: #0a0c10; object-fit: cover; }
  .names .zh { font-weight: 700; font-size: 14px; }
  .names .en { color: var(--muted); font-size: 12px; }
  .meta { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .auto-line { font-size: 11px; color: var(--muted); margin-top: 3px; }
  .roles { display: flex; flex-wrap: wrap; gap: 6px; }
  .role-btn {
    padding: 5px 10px; border-radius: 999px; border: 1px solid var(--border);
    background: #1a2030; color: var(--muted); font-size: 12px; font-weight: 600;
    cursor: pointer; user-select: none;
  }
  .role-btn.on {
    color: #0e1116; border-color: transparent;
  }
  .side {
    display: flex; flex-direction: column; gap: 6px; align-items: flex-end;
  }
  .side button.small {
    padding: 4px 8px; font-size: 11px; border-radius: 8px;
  }
  .badge {
    font-size: 10px; padding: 2px 6px; border-radius: 6px;
    background: #243044; color: var(--muted);
  }
  .badge.lut { background: #1e3a2f; color: #86efac; }
  .badge.extra { background: #3a2a12; color: #fcd34d; }
  footer-bar {
    position: fixed; bottom: 0; left: 0; right: 0;
    display: flex; gap: 8px; justify-content: center; flex-wrap: wrap;
    padding: 10px 16px;
    background: color-mix(in srgb, var(--bg) 92%, transparent);
    border-top: 1px solid var(--border);
    backdrop-filter: blur(10px);
  }
  @media (max-width: 720px) {
    .row {
      grid-template-columns: 40px 1fr;
      grid-template-areas:
        "icon names"
        "roles roles"
        "side side";
    }
    .icon { grid-area: icon; width: 40px; height: 40px; }
    .names { grid-area: names; }
    .roles { grid-area: roles; }
    .side { grid-area: side; flex-direction: row; justify-content: flex-start; }
  }
</style>
</head>
<body>
<header>
  <h1>裝備職業標註</h1>
  <div class="sub">
    點職業 chip 切換（可多選）。黃色框 = 已改過自動分類；紅框 = 尚無任何職業。
    進度會自動存 localStorage。完成後按「匯出 JSON」。
  </div>
  <div class="toolbar">
    <input type="search" id="q" placeholder="搜尋中/英名、ID、tag…" autocomplete="off">
    <select id="view">
      <option value="all">全部</option>
      <option value="lut" selected>站上有的（itemLut）</option>
      <option value="changed">已修改</option>
      <option value="unlabeled">未標註</option>
      <option value="diff-auto">與自動不同</option>
      <option value="Assassin">只看·刺客</option>
      <option value="Fighter">只看·戰士</option>
      <option value="Mage">只看·法師</option>
      <option value="Marksman">只看·射手</option>
      <option value="Support">只看·輔助</option>
      <option value="Tank">只看·坦克</option>
    </select>
    <button type="button" id="btn-accept-auto">接受目前可見自動分類</button>
    <button type="button" id="btn-clear-visible">清空目前可見標註</button>
    <label class="file-btn">匯入 JSON<input type="file" id="import" accept="application/json,.json" hidden></label>
    <button type="button" class="primary" id="btn-export">匯出 JSON</button>
    <span class="stats" id="stats"></span>
  </div>
  <div class="legend" id="legend"></div>
</header>
<main id="list"></main>
<footer-bar>
  <button type="button" class="primary" id="btn-export-2">匯出 JSON</button>
  <button type="button" id="btn-copy">複製 JSON 到剪貼簿</button>
  <button type="button" id="btn-reset-all">重置全部為自動</button>
</footer-bar>
<script>
const ITEMS = __ITEMS__;
const ROLE_ORDER = __ROLE_ORDER__;
const ROLE_ZH = __ROLE_ZH__;
const ROLE_COLOR = __ROLE_COLOR__;
// Seed from the last committed overrides file (if any); localStorage still wins.
const SEED_ANNOTATIONS = __SEED_ANNOTATIONS__;
const STORAGE_KEY = "arammeta-item-role-annotations-v1";

/** @type {Record<string, string[]>} */
let annotations = {};

function loadStored() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        annotations = parsed;
        return;
      }
    }
  } catch {}
  if (SEED_ANNOTATIONS && typeof SEED_ANNOTATIONS === "object") {
    annotations = { ...SEED_ANNOTATIONS };
  }
}
function saveStored() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(annotations)); } catch {}
}

function rolesEqual(a, b) {
  const sa = [...(a || [])].map(String).sort().join(" ");
  const sb = [...(b || [])].map(String).sort().join(" ");
  return sa === sb;
}

function currentRoles(item) {
  const key = String(item.id);
  if (Object.prototype.hasOwnProperty.call(annotations, key)) {
    return [...annotations[key]];
  }
  return [...(item.auto || [])];
}

function setRoles(item, roles) {
  const key = String(item.id);
  const cleaned = ROLE_ORDER.filter(r => roles.includes(r));
  // Always store explicit annotation once user touches it.
  annotations[key] = cleaned;
  saveStored();
}

function toggleRole(item, role) {
  const cur = new Set(currentRoles(item));
  if (cur.has(role)) cur.delete(role);
  else cur.add(role);
  setRoles(item, [...cur]);
}

function isChanged(item) {
  const key = String(item.id);
  if (!Object.prototype.hasOwnProperty.call(annotations, key)) return false;
  return !rolesEqual(annotations[key], item.auto || []);
}
function isExplicit(item) {
  return Object.prototype.hasOwnProperty.call(annotations, String(item.id));
}
function isUnlabeled(item) {
  return currentRoles(item).length === 0;
}

function exportObject() {
  // Full explicit map for every site item that has roles (prefer annotations).
  const out = {};
  for (const item of ITEMS) {
    const roles = currentRoles(item);
    if (roles.length) out[String(item.id)] = roles;
  }
  return {
    schema_version: 1,
    purpose: "item_role_filter_overrides",
    role_order: ROLE_ORDER,
    notes: "Keys are item ids. Values are site role chips (multi ok). Empty list = hide from all role filters.",
    overrides: Object.fromEntries(
      Object.entries(annotations).map(([k, v]) => [k, ROLE_ORDER.filter(r => v.includes(r))])
    ),
    full_map: out,
  };
}

function downloadJson() {
  const blob = new Blob([JSON.stringify(exportObject(), null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "item-role-overrides.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

function copyJson() {
  const text = JSON.stringify(exportObject(), null, 2);
  navigator.clipboard.writeText(text).then(() => {
    alert("已複製到剪貼簿");
  }).catch(() => {
    prompt("複製以下 JSON：", text);
  });
}

function matchView(item, view, q) {
  if (view === "lut" && !item.inLut) return false;
  if (view === "changed" && !isChanged(item)) return false;
  if (view === "unlabeled" && !isUnlabeled(item)) return false;
  if (view === "diff-auto" && rolesEqual(currentRoles(item), item.auto || [])) return false;
  if (ROLE_ORDER.includes(view) && !currentRoles(item).includes(view)) return false;
  if (q) {
    const blob = [
      item.id, item.zh, item.en, ...(item.cats || []), ...(item.auto || []), ...currentRoles(item),
    ].join(" ").toLowerCase();
    if (!blob.includes(q)) return false;
  }
  return true;
}

function renderLegend() {
  const el = document.getElementById("legend");
  el.innerHTML = ROLE_ORDER.map(r =>
    `<span style="border-color:${ROLE_COLOR[r]};color:${ROLE_COLOR[r]}">${ROLE_ZH[r]} ${r}</span>`
  ).join("") + `<span>共 ${ITEMS.length} 件 · 站上 ${ITEMS.filter(i => i.inLut).length} 件</span>`;
}

function render() {
  const q = (document.getElementById("q").value || "").trim().toLowerCase();
  const view = document.getElementById("view").value;
  const list = document.getElementById("list");
  const frag = document.createDocumentFragment();
  let shown = 0, changed = 0, unlabeled = 0;

  for (const item of ITEMS) {
    if (!matchView(item, view, q)) continue;
    shown++;
    const roles = currentRoles(item);
    if (isChanged(item)) changed++;
    if (isUnlabeled(item)) unlabeled++;

    const row = document.createElement("div");
    row.className = "row"
      + (isChanged(item) ? " changed" : "")
      + (isUnlabeled(item) ? " unlabeled" : "");
    row.dataset.id = String(item.id);

    const img = document.createElement("img");
    img.className = "icon";
    img.loading = "lazy";
    img.src = item.icon;
    img.alt = "";
    img.onerror = () => { img.style.opacity = "0.25"; };

    const names = document.createElement("div");
    names.className = "names";
    names.innerHTML = `
      <div class="zh">${esc(item.zh || item.en || ("#" + item.id))}</div>
      <div class="en">${esc(item.en || "")} · <code>${item.id}</code> · ${item.price || "?"}g</div>
      <div class="meta">${esc((item.cats || []).join(" · ") || "no tags")}</div>
      <div class="auto-line">自動：${(item.auto && item.auto.length) ? item.auto.map(r => ROLE_ZH[r] || r).join("、") : "（無）"}${isExplicit(item) ? " · 已手動" : ""}</div>
    `;

    const rolesEl = document.createElement("div");
    rolesEl.className = "roles";
    for (const r of ROLE_ORDER) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "role-btn" + (roles.includes(r) ? " on" : "");
      btn.textContent = ROLE_ZH[r] || r;
      if (roles.includes(r)) {
        btn.style.background = ROLE_COLOR[r];
        btn.style.color = "#0e1116";
      }
      btn.addEventListener("click", () => {
        toggleRole(item, r);
        render();
      });
      rolesEl.appendChild(btn);
    }

    const side = document.createElement("div");
    side.className = "side";
    const badge = document.createElement("span");
    badge.className = "badge " + (item.inLut ? "lut" : "extra");
    badge.textContent = item.inLut ? "站上" : "額外";
    side.appendChild(badge);

    const accept = document.createElement("button");
    accept.type = "button";
    accept.className = "small";
    accept.textContent = "用自動";
    accept.title = "把此件標成目前自動分類";
    accept.addEventListener("click", () => {
      setRoles(item, [...(item.auto || [])]);
      render();
    });
    side.appendChild(accept);

    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "small";
    clear.textContent = "清空";
    clear.addEventListener("click", () => {
      setRoles(item, []);
      render();
    });
    side.appendChild(clear);

    row.appendChild(img);
    row.appendChild(names);
    row.appendChild(rolesEl);
    row.appendChild(side);
    frag.appendChild(row);
  }

  list.innerHTML = "";
  list.appendChild(frag);
  document.getElementById("stats").textContent =
    `顯示 ${shown} · 已改 ${changed} · 未標 ${unlabeled} · 手動 ${Object.keys(annotations).length}`;
}

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function visibleItems() {
  const q = (document.getElementById("q").value || "").trim().toLowerCase();
  const view = document.getElementById("view").value;
  return ITEMS.filter(item => matchView(item, view, q));
}

document.getElementById("q").addEventListener("input", render);
document.getElementById("view").addEventListener("change", render);
document.getElementById("btn-export").addEventListener("click", downloadJson);
document.getElementById("btn-export-2").addEventListener("click", downloadJson);
document.getElementById("btn-copy").addEventListener("click", copyJson);
document.getElementById("btn-accept-auto").addEventListener("click", () => {
  for (const item of visibleItems()) setRoles(item, [...(item.auto || [])]);
  render();
});
document.getElementById("btn-clear-visible").addEventListener("click", () => {
  if (!confirm("清空目前可見項目的職業標註？")) return;
  for (const item of visibleItems()) setRoles(item, []);
  render();
});
document.getElementById("btn-reset-all").addEventListener("click", () => {
  if (!confirm("重置全部手動標註（回到自動分類）？")) return;
  annotations = {};
  saveStored();
  render();
});
document.getElementById("import").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const src = data.overrides || data.full_map || data;
    if (!src || typeof src !== "object") throw new Error("bad format");
    for (const [k, v] of Object.entries(src)) {
      if (Array.isArray(v)) annotations[String(k)] = ROLE_ORDER.filter(r => v.includes(r));
      else if (typeof v === "string") annotations[String(k)] = ROLE_ORDER.filter(r => v.split(/\s+/).includes(r));
    }
    saveStored();
    render();
    alert("匯入完成");
  } catch (e) {
    alert("匯入失敗：" + e);
  }
  ev.target.value = "";
});

loadStored();
renderLegend();
render();
</script>
</body>
</html>
"""


def main() -> None:
    items = collect_items()
    # Prefer hand overrides as annotator seed; fall back to full_map snapshot.
    label_file = load_item_role_filter_label_file()
    seed = {}
    if isinstance(label_file.get("overrides"), dict):
        seed.update({str(k): v for k, v in label_file["overrides"].items() if isinstance(v, list)})
    # full_map not seeded as "manual" so only true hand-diffs show as changed
    html = (
        HTML_TEMPLATE
        .replace("__ITEMS__", json.dumps(items, ensure_ascii=False, separators=(",", ":")))
        .replace("__ROLE_ORDER__", json.dumps(ROLE_ORDER, ensure_ascii=False))
        .replace("__ROLE_ZH__", json.dumps(ROLE_ZH, ensure_ascii=False))
        .replace("__ROLE_COLOR__", json.dumps(ROLE_COLOR, ensure_ascii=False))
        .replace("__SEED_ANNOTATIONS__", json.dumps(seed, ensure_ascii=False, separators=(",", ":")))
    )
    out = ROOT / "exports" / "item-role-annotator.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({len(items)} items, {sum(1 for i in items if i['inLut'])} in itemLut)")
    print(f"seed overrides: {len(seed)}")
    print(f"open: {out.resolve()}")


if __name__ == "__main__":
    main()
