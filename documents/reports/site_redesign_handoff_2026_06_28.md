# ARAM Mayhem 網站改版 — 交接文件 (2026-06-28)

> **Archived / superseded:** 本檔描述 2026-06-28 的單檔 builder 與四分頁狀態。現行設計 owner 是根目錄 `../../DESIGN.md`，部署流程是 `../../runbooks/site-deploy.md`，source 已拆成 templates／engine／renderer。

> 把 tier-list 網站從「單頁英雄榜」改成「固定眉頭 + 分頁 SPA + 深淺主題 + 視覺精修」。
> 本文件給接手者（或未來的自己）：架構、擴充點、雷區、未完成項（附可直接用的程式碼）。

---

## 0. 一句話總結

`scripts/build_tier_list.py`（單檔 Python 產生器）現在輸出一個有**固定眉頭 + 4 分頁（主頁 / 增幅榜 / 專欄 / 設定）**、**深/淺主題切換**、以及一輪 op.gg/Linear 風格視覺精修的 `docs/index.html`。已實機驗證深淺兩主題、無 console 錯誤。

---

## 1. Source of truth / build / deploy（最重要）

- **唯一真實來源 = `scripts/build_tier_list.py`。** 所有 HTML / CSS / JS 都內嵌在這支 Python 的三引號字串裡。
- **絕對不要手改 `docs/index.html`** — watchdog 會用 working tree 重建覆蓋掉（見 memory `deploy_site_fix_workflow` / `crawler_run_mode_watchdog`）。改 CSS/JS 一律改 Python 來源。
- **重建指令**：
  ```bash
  python scripts/build_tier_list.py --site-url "https://lanternko.github.io/ARAM-Mayhem-Database/" \
      --payload-out docs/api/tier-list.json --payload-url api/tier-list.json
  ```
  讀 18GB 的 `data/lcu/games.db`，跑完約 **15 分鐘**（會做全部 affinity / synergy 計算）。
- **部署**：MayhemLCUWatchdogKeepalive 排程工作會自動 rebuild + commit + push 到 GitHub Pages（`Lanternko/ARAM-Mayhem-Database` branch `main` `/docs`）→ https://lanternko.github.io/ARAM-Mayhem-Database/ 。也可手動跑 `/deploy-tier-list` skill。**本次改版未手動 commit**，靠 watchdog 上線。
- **本機預覽**：`.claude/launch.json` 的 `tier-list-docs`（python http.server，port 8099）→ 開 `http://localhost:8099/index.html`。

> Python 字串內嵌 JS 是 **一般字串**（非 raw、非 f-string）：反斜線要寫兩次（`\\s`），但 `{`/`}`/`${...}` 是字面值。`__PAYLOAD__` 等 placeholder 之後用 `.replace()` 代換。

---

## 2. 新架構

### 2.1 版面骨架（HTML，在 `render_html()` 內 `parts.append(...)`）
```
<header class="site-header">
  <div class="site-header-inner">           ← max-width: var(--container) 1320px
    <button class="brand" data-nav-tab="home"> dice logo + 標題 + .brand-patch 版本chip </button>
    <nav class="nav-tabs" role="tablist">     ← 4 個 .nav-tab，roving tabindex
    <div class="header-actions"> .gh-star (ghost) </div>
<main class="site-main">                      ← max-width: var(--container)
  <section class="view view-home is-active" id="view-home">   ← 既有 app-shell（英雄榜 + 推薦側欄）
  <section class="view view-augments" id="view-augments">     ← #aug-tier-filters + #aug-tier-host（JS renderAugmentTier，見 §5.1）
  <section class="view view-column"  id="view-column">        ← #column-host（JS 渲染）
  <section class="view view-settings" id="view-settings">     ← 語言 / 主題 / 更新 / 關於
```
- **顯示控制**：`.view { display:none }` + `.view.is-active { display:block; animation:viewIn }`。
- **分頁切換**：JS `setActiveView(name)`（toggle active class / aria-selected / roving tabindex / `.is-active` / hash / scrollTo）。
- **路由**：hash（`#home`/`#augments`/`#column`/`#settings`），可深連結；`hashchange` listener + 初始化讀 hash。
- **鍵盤**：方向鍵 / Home / End 在 `.nav-tabs` 上切分頁（WAI-ARIA tabs）。
- **brand logo** 也帶 `data-nav-tab="home"` → 點 logo 回主頁（但不是 `.nav-tab`，不會被加 active）。

### 2.2 主題系統
- Token 在 `:root` + `:root[data-theme="light"]`（顏色、`--ease`/`--dur-*` 動效、`--r-*` 圓角、`--container`）。
- JS `applyTheme(theme)`：設 `data-theme`、寫 `localStorage[THEME_KEY]`、更新 `#theme-seg` 的 active/aria-pressed。
- **`.no-theme-transition` guard**：切主題瞬間關掉所有 transition 一拍（`setTimeout 60ms` 移除）。
- ⚠️ **雷區**：`transition: background`（shorthand）在 CSS 變數調色盤翻轉時會**永久卡在舊色**（引擎無法內插 var 代換）。互動元件一律用 `transition: background-color`。已在 CSS 註解標明，**不要改回 shorthand**。
- **淺色是 first-pass**：眉頭/卡片/設定/專欄都用 var 上色，深淺都正確；但 detail 詳情面板 / 推薦側欄維持「深色卡片（強制淺色文字）」，沒有完整淺色化（見 §5.3）。

### 2.3 專欄文章
- 資料 = JS `const ARTICLES`（陣列，**新的放最前**）。每篇：`id` / `date` / `kicker_zh|en` / `title_zh|en` / `summary_zh|en` / `body_zh|en`（body 是信任 HTML，其餘 render 時 escHtml）。
- 渲染：`renderColumnList()`（卡片清單）/ `renderArticle(id)`（單篇 + 返回鈕）/ `articleField(a, base)`（依語言取欄位）。state = `let columnArticle`（null = 清單）。
- 語言切換時若在專欄頁會自動重渲染（`applyLanguage` 內）。
- **互動圖表型文章**（2026-06-28 新增 `id: 'scaling-snowball'`「後期 × 滾雪球定位圖」）：`body` 用 innerHTML 注入，**`<script>` 不會執行**，所以圖是用「host div + 後渲染鉤子」模式。body 內放 `<div id="scatter-host">`，`renderArticle()` 設完 innerHTML 後偵測到該 div 就呼叫 `renderScalingSnowballChart()`。該函式直接吃 `DATA.champs[*].comp.scaling/snowball`（由 `champ-empirical-axes.json` 在啟動時 merge 進來）+ `info.image/wr/g`，在 JS 端跑碰撞防疊後輸出「SVG 背景層（四象限/中位線/座標）＋ 絕對定位的圓形頭像（外框＝勝率色）」。用 `ResizeObserver` 寬度變化才重畫；`renderColumnList()` / `renderArticle()` 開頭會 `_scatterRO.disconnect()` 避免洩漏。要再加同類圖表文章就照這個鉤子模式。

### 2.4 設定頁
- 從眉頭搬進來的：`#lang-toggle`（沿用既有 delegated handler）、`#theme-seg`（新 segmented）、`#updates-toggle` + `#updates-panel`。
- **#updates-panel 的搬移**：它先被 emit 在 home view 裡（避開它那串亂碼 placeholder bytes），init 時 `relocateUpdatesPanel()` 用 JS 把節點 append 到設定頁的 `#updates-host`。**改更新內容請改 `renderUpdatesPanel()` / COPY**。
- 關於卡：資料佇列 / 版本 / 更新時間 / GitHub / 免責聲明（資料來自 render_html scope 的 `queue_label` / `queue_id` / `display_patch` / `build_date` / `total_games` / `REPO_URL`）。

### 2.5 設計 token（`:root`）
```
顏色：--bg --surface --surface-2 --surface-sunken --surface-raised --chip-bg --overlay
      --text --text-muted --text-dim --border(半透明白) --border-strong
      --accent(金 #f5c518 / 淺色 #e0a500) --accent-soft --accent-on
動效：--ease / --ease-out = cubic-bezier(.16,1,.3,1)、--ease-spring、--dur-1..4 (120/180/240/320ms)
形狀：--r-sm 8 / --r-md 12 / --r-lg 16、--container 1320px、--header-h 56px
```

---

## 3. 怎麼擴充（常見任務）

| 要做的事 | 怎麼做 |
|---|---|
| **加一篇專欄文章** | 在 `ARTICLES` 陣列最前面加一筆（雙語欄位齊全）。重建即可。 |
| **加一個分頁** | (1) `nav_items` loop 加一筆 `(key, zh, en)`；(2) 加一個 `<section class="view view-X" id="view-X" data-view="X" role="tabpanel" aria-labelledby="tab-X">`；(3) `const VIEWS` 加 `'X'`；(4) 若需動態內容，在 `setActiveView` 加 render 分支。 |
| **改主題顏色** | 改 `:root` / `:root[data-theme="light"]` 的 token，別硬寫 hex 在元件上。 |
| **改更新紀錄** | `renderUpdatesPanel()` + `COPY.zh/en.updatesItems`（注意 panel 已搬到設定頁）。 |
| **加新主題（如護眼）** | 加 `:root[data-theme="sepia"]` token；`#theme-seg` 加一顆 `data-theme-choice="sepia"`；`applyTheme` 已是通用的。 |

---

## 4. Codex 審查（已全部修掉，供參考）

| 嚴重度 | 問題 | 修法 |
|---|---|---|
| **major** | 設定頁的更新「英雄變動列」`[data-change-cid]` 開 detail 時沒先切 home → 開在隱藏 view，使用者看不到 | `openDetailByCid()` 內先呼叫 `setActiveView('home')` |
| minor | Ctrl/Cmd+F 在任何分頁都搶 focus 到（隱藏的）搜尋框 | 只在 home view active 時攔截，其餘走原生 find |
| minor | 文章卡用 `<button>` 包 `<h3>/<p>`（非法 HTML） | 改 `<div role="button" tabindex="0">` + 既有 Enter/Space keydown handler 加 `.article-card` |
| minor | nav 缺 `aria-controls`/tablist 鍵盤 | 補 `id=tab-X` / `aria-controls` / `aria-labelledby` / roving tabindex / 方向鍵 |
| minor | 主題 segmented 缺 `aria-pressed` | HTML 加初值 + `applyTheme` 同步 |

---

## 5. 未完成 / TODO（接手重點）

### 5.1 增幅榜（Augment Tier）接真資料 — ✅ 已實作並驗證（2026-06-28）

`#view-augments` 不再是佔位，已是真資料 tier 榜（206 個 augment rollup，過門檻者 203 個上榜，6 階金字塔 OP16/T1 35/T2 51/T3 51/T4 34/T5 16）。

**做法**
- `build_augment_global_stats()`（放在 `posterior_wr_summary` 旁）把 `champ_aug` 依 `augment_id` 聚合（sum games/wins）成全域 augment 勝率，套 `posterior_wr_summary()` EB 收縮（prior=`AUGMENT_PRIOR_DEFAULT`、baseline=整體 augment 勝率≈0.498），附「同稀有度內」選用率 share。在 `render_html` 呼叫點算好後 merge 進既有 `augs` payload（每筆多 `wr/rawWr/g/lcb/lift/pick`）；payload 另加 `tiers`（order + 每階顏色）讓前端 client-render 的 tier-block 直接重用英雄那套配色。
- 前端 `renderAugmentTier()`（重用 `buildAugCard` + `.tier-block`）：rarity 篩選 chip（全部/棱彩/金/銀，單選 AND）＋ 既有分類 chip（多選 OR）就地 show/hide；掛在 `setActiveView('augments')` 與語言切換重渲染。

**關鍵決策（給下一手）**
1. **分級在 JS 端算（within-rarity 百分位），不在 Python。** 昂貴的是 DB rollup（=payload，已固化）；分級規則放 JS 常數 `AUG_TIER_PCT`（OP 8%/T1 17%/T2 25%/T3 25%/T4 17%/T5 8%），要調手感改這裡重載即可，**不必再跑 15 分鐘 build**。
2. **為何 within-rarity 而非絕對勝率分級**：augment 勝率被稀有度嚴重干擾——實測 prismatic 0.395–0.590（最散）、gold 0.434–0.549、silver 0.452–0.553（最窄），頂級 silver(0.553) 還高過 prismatic 中位(0.488)。絕對分級會把強 silver 埋掉、S 階全是 prismatic（沒資訊量）。改「同稀有度內百分位」→ 每稀有度都有完整 OP→T5；驗證：篩 prismatic → 69 張散在 6 階。
3. **門檻 `AUG_TIER_MIN_GAMES=500`（JS 常數）** 只剔 3 個極低樣本；其餘場數 3k–122k。
4. **chip 事件雷**：tier chip 也掛 `.aug-cat-chip`（共用樣式），所以舊的 detail filter handler 加了 `chip.closest('#aug-tier-filters')` 早退避免誤觸；tier 自己的 handler 認 `data-rarity` / `data-tcat`（非 `data-cat`）。
- 預設 queue 2400（見 memory `feedback_mayhem_default`）。

### 5.2 三項「平滑度」精修 — ✅ 已實作並驗證（2026-06-28）

(A) 分頁切換 View Transition、(B) 滑動式分頁指示器、(C) 設定 segmented 滑動 thumb 都已上線（source = `build_tier_list.py`，並同步 mirror 進 `docs/index.html` 供 preview；watchdog 之後會用同一份 source 重建 docs）。實作時為了踩過的雷而偏離了原草稿，原因記在這給下一手 —— **這四點比程式碼本身重要**：

1. **指示器是真的 `<span class="nav-ind">`，不是 `::after`。** 一個 View Transition 進行時，沒掛 `view-transition-name` 的元素會被凍結成 **root 快照的靜態點陣圖**，底線的滑動會被蓋住看不到。所以底線必須是「具名」VT 元素（`view-transition-name: tab-ind`）才能浮在 root 快照之上 morph；而 `::after` 無法穩定掛 VT name，故改用真元素。panel 用 `view-transition-name: view`、root 用 `animation:none` 釘住（眉頭不閃、切頁的 `scrollTo` 不會拖動整頁）。
2. **`.ui-ready` 必須同步開啟（reflow + class add），不要用 `requestAnimationFrame`。** 分頁未繪製時（背景分頁載入、或無頭 preview）rAF callback 會被擱置 → 永遠不加 `.ui-ready` → 指示器/thumb 的 transition 永遠 `none`（不會滑）。改成 init 內「先定位 → 強制 reflow → 加 `.ui-ready`」全同步。
3. **VT 進行中用 `.vt-running` 關掉指示器/thumb 的 CSS transition。** 否則 `apply()` 改 `--ind-x` 時 CSS tween 與 VT 同時搶，新快照在 tween 起點就拍照 → 指示器卡在半路（這正是第一版的 bug）。`setActiveView` 在 `startViewTransition(apply)` 前加 `.vt-running`、`.finished` 後移除；`.vt-running .nav-ind{transition:none}` 讓真元素瞬間跳到終點供新快照拍照，再由 VT 做可見 morph。
4. **`moveSegThumb` 預設 snap + 隱藏時跳過。** segmented 在隱藏的設定頁裡，`offsetWidth=0` 量不到 → thumb 會被設成寬 0。故 `moveSegThumb` 在 `!active.offsetWidth` 時直接 return（保留上次幾何），預設用 inline `transition:none` snap，只有 `applyTheme` 切主題時傳 `animate=true` 才滑；切到設定頁後在 VT `.finished` 再 snap 一次確保正確。

**驗證**（preview port 8201，1280×800）：4 個分頁切換指示器皆精準對位（`indMatchesTab` 全 true）、設定頁 thumb 對齊作用中選項、深/淺主題互切 + `aria-pressed` 正確、4 個 view 都正常渲染、無 console error。⚠️ 滑動「動畫本身」在無頭 preview **不繪製**（rAF / CSS transition / 截圖一律凍結在起始幀），只能驗證幾何與終態邏輯；真實瀏覽器（會 paint）才看得到滑動。

**程式位置**：`setActiveView`（內含 `apply()` / `startViewTransition` / `.vt-running`）、`moveTabIndicator`、`moveSegThumb`、init（同步 `.ui-ready`）；CSS `.nav-ind` / `.seg-thumb` / `.vt-running …{transition:none}` / `::view-transition-*` / `.view.is-active{view-transition-name:view}`。

### 5.3 淺色模式 detail / 側欄精修
詳情面板 / 推薦側欄目前在淺色下是「深色卡片」。要完整淺色化需逐一處理其密集內部（很多硬寫的淺灰文字）—— 工作量中等，目前是刻意的 first-pass scope。

---

## 6. 關鍵程式位置（用名稱找，行號會位移）

| 名稱 | 作用 |
|---|---|
| `render_html()` | 產生整頁；body 組裝在 `parts.append(...)`（眉頭 / main / 4 views） |
| `css = """..."""` | 全部 CSS（`:root` token、`.site-header`、`.nav-tab`、`.view`、`.article-card`、`.coming-soon`、`[data-theme="light"]` 覆寫） |
| `js = """..."""` | 全部 JS |
| `setActiveView` / `applyTheme` / `applyLanguage` | 分頁 / 主題 / 語言 三大切換 |
| `renderColumnList` / `renderArticle` / `articleField` / `ARTICLES` | 專欄 |
| `relocateUpdatesPanel` | 把 #updates-panel 搬進設定頁 |
| `setActiveView` 內的 `[data-nav-tab]` click delegation + keydown | 分頁互動 |
| `VIEWS` / `THEME_KEY` / `LANG_KEY` | 常數 |

---

## 7. 研究背景（供之後再精修時參考）

- **Obsidian vault（`D:\obsidian-vault`）沒有任何 UI/設計素材**（2026-06-28 徹底搜過，只有 NBA / 版型 / 哲學雜訊）。之後要找設計靈感別再翻 vault。
- 視覺方向採線上研究 + 美學稽核綜整的 **15 項計畫**（op.gg/Linear「engineered calm」）。15 項全數套用（最後 3 項平滑度精修於 2026-06-28 補齊，見 §5.2）。
- 相關 memory：`site_nav_redesign`、`deploy_site_fix_workflow`、`feedback_mayhem_default`、`crawler_run_mode_watchdog`、`recommender_run_from_source`。

---

## 8. 驗證 checklist（改動後跑一遍）
1. `python -m py_compile scripts/build_tier_list.py`
2. 重建 → `node --check`（抽出最大 inline `<script>`）+ CSS `{`/`}` 數量平衡
3. `tier-list-docs` server（8099）→ `preview_eval` 檢查：4 tabs、4 views、深/淺主題 body bg、無重複 id、`#updates-panel` parent = `updates-host`
4. `preview_screenshot` 深色 + 淺色各一張
5. `preview_console_logs level=error` 應為空
