---
name: "arammeta"
description: "Calibrated ARAM and Mayhem intelligence in an engineered-calm interface."
colors:
  canvas-dark: "#0a0b0d"
  surface-dark: "#101114"
  surface-raised-dark: "#1a1d21"
  text-dark: "#e8eaed"
  text-muted-dark: "#9aa0a6"
  border-dark: "#ffffff17"
  accent-gold-dark: "#f5c518"
  accent-gold-soft-dark: "#f5d780"
  accent-on: "#14110a"
  canvas-light: "#f3f5f9"
  surface-light: "#ffffff"
  surface-soft-light: "#eef0f3"
  text-light: "#1a1e26"
  text-muted-light: "#5b6472"
  border-light: "#0000001a"
  accent-gold-light: "#e0a500"
  accent-gold-soft-light: "#9a7100"
  tier-op: "#d8b8ff"
  tier-t1: "#ff5a3c"
  tier-t2: "#f5c518"
  tier-t3: "#8ec441"
  tier-t4: "#3aa0ff"
  tier-t5: "#7a7f8a"
  role-assassin: "#ef4444"
  role-fighter: "#f97316"
  role-mage: "#3b82f6"
  role-marksman: "#22c55e"
  role-support: "#ec4899"
  role-tank: "#a855f7"
typography:
  wordmark:
    fontFamily: "Outfit, Noto Sans TC, sans-serif"
    fontSize: "26px"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.1
  body:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.25
  caption:
    fontFamily: "Noto Serif TC, Source Han Serif TC, PMingLiU, serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  data:
    fontFamily: "Noto Sans TC, Segoe UI, Microsoft JhengHei, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1.2
rounded:
  sm: "8px"
  md: "12px"
  lg: "16px"
  pill: "999px"
spacing:
  xxs: "4px"
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "24px"
  xl: "32px"
  page-bottom: "64px"
components:
  wordmark:
    textColor: "{colors.text-dark}"
    typography: "{typography.wordmark}"
    height: "26px"
  nav-tab-active:
    backgroundColor: "{colors.surface-raised-dark}"
    textColor: "{colors.text-dark}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  button-primary:
    backgroundColor: "{colors.accent-gold-dark}"
    textColor: "{colors.accent-on}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "10px 14px"
  button-quiet:
    backgroundColor: "{colors.surface-raised-dark}"
    textColor: "{colors.text-dark}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  filter-chip:
    backgroundColor: "{colors.surface-raised-dark}"
    textColor: "{colors.text-muted-dark}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "7px 10px"
  data-panel:
    backgroundColor: "{colors.surface-dark}"
    textColor: "{colors.text-dark}"
    rounded: "{rounded.md}"
    padding: "16px"
  search-input:
    backgroundColor: "{colors.surface-dark}"
    textColor: "{colors.text-dark}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
    height: "40px"
  tier-tile:
    backgroundColor: "{colors.surface-raised-dark}"
    textColor: "{colors.text-dark}"
    rounded: "{rounded.sm}"
    padding: "0px"
---

# Design System: arammeta

## Overview

**Creative North Star: "Engineered Calm"**

arammeta 是一套玩家會在選角前、遊戲中或賽後快速掃讀的決策儀器。預設深色畫布配合昏暗的遊戲環境，低彩度 surface 讓高密度資料保持安靜，金色只出現在需要做事或確認目前位置的地方。Light theme 是同一套產品語意的日間版本，不是另一個品牌。

資訊順序固定為答案、證據、可信度。畫面先讓玩家看見 tier、選擇或版本變化，再補勝率、lift、樣本數、patch 與限制。每個 view 可以密集，但不能吵雜；一個平面 panel 以 hairline 分區，比一疊互相包覆的卡片更能呈現資料關係。

系統明確拒絕高彩度電競海報、霓虹 cyberpunk、紫色奢華、玻璃擬態、巨大 glow、裝飾性 gradient text、generic SaaS landing page 與 card-in-card 儀表板。視覺不是用來替統計製造確定感，而是幫使用者更快看出層級、狀態與證據。

**Key Characteristics:**

- 深色預設、明亮可切換、單一語意 token 系統。
- 高密度、低干擾、數值優先、細節按需載入。
- 一個主要金色 accent，tier 與 role 色只負責分類。
- 1320px 桌面 rail、56px header、24px page gutter。
- 五個主要產品分頁，導覽位置與統計語意跨語系一致。
- 熟悉的 tab、chip、search、panel 與 detail 行為。
- 120ms 至 320ms 的狀態轉場，完整支援 reduced motion。

**The Answer-Evidence-Trust Rule.** 每個資料視圖必須依序回答「該看什麼」、「為什麼」與「能否相信」，不可先堆 metadata 再讓玩家自己找結論。

**The One-Rail Rule.** 主要內容固定在 1320px rail 內，頁面水平 padding 為 24px，窄版閱讀內容可縮到 920px，長文閱讀寬度上限為 720px。

## Colors

色彩以近黑炭灰、冷 slate 與克制金色構成。Dark theme 的核心 token 是 `canvas-dark`、`surface-dark`、`surface-raised-dark`、`text-dark` 與 `accent-gold-dark`；Light theme 以對應的 light token 保留相同層級，不做機械反相。

### Primary

- **Signal Gold:** `accent-gold-dark` 用於 active underline、主操作、選取確認與少量品牌互動。Light theme 改用 `accent-gold-light` 以維持對比。
- **Soft Gold:** `accent-gold-soft-dark` 與 `accent-gold-soft-light` 只用在 accent 文字、次級提示或較安靜的狀態，不作大面積背景。
- **Ink on Gold:** `accent-on` 是金色填滿元件上的前景色，禁止直接使用白字壓在金色上。

### Secondary

- **Win-rate tiers:** `tier-op`、`tier-t1`、`tier-t2`、`tier-t3`、`tier-t4`、`tier-t5` 只編碼勝率分級。OP 可使用低速稜彩邊框；T1 可使用受控的紅橘漸層邊框；T2 至 T5 使用單色。
- **Champion roles:** Assassin、Fighter、Mage、Marksman、Support、Tank 分別使用 `role-assassin`、`role-fighter`、`role-mage`、`role-marksman`、`role-support`、`role-tank`。角色色只出現在 chip、dot、細邊框或小型標籤，不能染滿 panel。

### Neutral

- **Charcoal Canvas:** `canvas-dark` 是預設頁面底色；頁尾可加入極淡冷 slate wash，但不能形成紫色 radial glow。
- **Quiet Surfaces:** `surface-dark` 與 `surface-raised-dark` 建立一級與二級 surface。`canvas-light`、`surface-light` 與 `surface-soft-light` 是日間對應層級。
- **Readable Ink:** `text-dark` 與 `text-light` 用於主要答案，muted token 用於 metadata。低對比文字不得承載關鍵勝率或可操作標籤。
- **Hairline Boundaries:** `border-dark` 與 `border-light` 用於 1px 邊界與分隔線。它們建立結構，不製造盒子感。

### Win-rate grading

英雄榜固定使用六級視覺順序：OP、T1、T2、T3、T4、T5。目前 Bayesian 調整後勝率門檻為 OP 至少 55%、T1 至少 52%、T2 至少 50%、T3 至少 48%、T4 至少 46%、T5 低於 46%。增幅榜在同 rarity 內依 percentile 分級，樣本下限與桶比例由 `scripts/tierlist_engine.py` 管理。

色彩文件只定義分級的視覺映射，不能定義統計演算法。任何 threshold、sample floor、shrinkage 或 percentile 修改，都必須先改 engine、測試與說明，再由 renderer 輸出 tier。

**The Gold Rarity Rule.** 金色在單一畫面保持稀少，常態目標不超過可視面積的 10%。如果所有重點都是金色，畫面就沒有重點。

**The Classification-Only Rule.** Tier 色與 role 色只作分類，不能兼任 CTA、錯誤訊息或選取狀態。

**The No Traffic-Light Rule.** 正負勝率使用低彩度文字、數值符號與位置共同表達，禁止只以紅綠色宣告好壞。

## Typography

**Display Font:** Outfit，fallback 為 Noto Sans TC 與 sans-serif

**Body Font:** Noto Sans TC，fallback 為 Segoe UI、Microsoft JhengHei、PingFang TC 與 sans-serif

**Caption Font:** Noto Serif TC，fallback 為 Source Han Serif TC、PMingLiU 與 serif

**Character:** Outfit 只賦予 arammeta 字標乾淨、幾何且略微收斂的輪廓。Noto Sans TC 承擔所有高密度 UI 與多語內容。Noto Serif TC 只在少量 subtitle、細節副標與 metadata caption 提供閱讀節奏，不能進入控制項或大面積正文。

### Wordmark

字標固定寫成小寫 `arammeta`，不在 header 加圖示或 patch chip。`aram` 使用 500 weight 與 muted text，`meta` 使用 700 weight 與 primary text；兩者共享 Outfit、緊縮字距與連續基線。`meta` 不改成金色，因為金色保留給互動焦點。整個字標是回到首頁的單一可操作元素，favicon 是另一個資產，不得混入字標。

桌面字標為 26px，行動版縮為 22px，line-height 固定為 1，letter-spacing 為 -0.035em。不可使用全大寫 `ARAM META`、斜體電競字、描邊、發光或拆成兩個按鈕。

### Hierarchy

- **Wordmark**，600、26px、1：只用於 global header，內部以 500 與 700 建立 `aram` 和 `meta` 對比。
- **Headline**，600、22px、1.1：頁面標題與主要 panel 標題，句子短、直接回答當前 view。
- **Body**，400、14px、1.5：控制說明、內容摘要與證據文字；長文段落限制在約 65ch 至 75ch。
- **Label**，600、12px、1.25：tab、chip、button、tier 與短 metadata label，不濫用 uppercase。
- **Caption**，400、13px、1.45：僅限 subtitle、detail 小標與 augment lift 或 games 列。
- **Data**，700、12px、1.2：勝率、lift、games、rank 與 tier 數值，必須啟用 tabular figures。

Google Fonts 以非阻塞方式載入 Outfit 500、600、700，Noto Sans TC 400、500、600、700，以及 Noto Serif TC 400、500。字型未完成下載時，fallback 仍須保持內容可讀與 layout 穩定。

**The UI Sans Rule.** 任何會被點擊、篩選、排序或快速比較的文字一律使用 sans；serif 只提供少量閱讀停頓。

**The Numeric Column Rule.** 可比較數值必須使用 tabular figures、固定小數語意與穩定對齊，禁止因數字寬度造成列表跳動。

## Elevation

arammeta 採 tonal layering 加低透明 hairline 的混合深度。Surface 預設是平的，只有 global overlay、detail panel 或需要與背景分離的主要資料 panel 使用柔和 ambient shadow。深度來自亮度差、邊界與間距，不來自厚重陰影或玻璃模糊。

### Shadow Vocabulary

- **Panel ambient** (`0 14px 36px rgba(0, 0, 0, 0.22)`): Dark theme 的大型資料 panel、detail surface 或浮層。
- **Panel ambient light** (`0 10px 32px rgba(31, 41, 55, 0.10)`): Light theme 對應層級。
- **Overlay** (`0 18px 50px rgba(0, 0, 0, 0.50)`): 只用於真正離開 document flow 的 overlay，不可套在每張資料卡。
- **Overlay light** (`0 18px 50px rgba(31, 41, 55, 0.16)`): Light theme 的 overlay。

Panel 使用 8px、12px、16px 三級圓角。8px 屬於 button、input、tile 與緊湊控制項；12px 屬於主要 panel；16px 只給大型 overlay 或特殊容器。Pill 只屬於 chip、badge 與 segmented control，不可把每個容器做成膠囊。

**The Flat-by-Default Rule.** Resting surface 不使用陰影。只有需要跨越內容層的 panel 或 overlay 才能升高。

**The One-Panel Rule.** 一個功能區使用一個 flat panel，內部以 1px hairline 和 12px 至 24px spacing 分組，禁止 card-in-card-in-card。

## Components

元件的共同氣質是「克制而明確」。控制項在 rest 狀態安靜，hover、focus、selected 才增加對比；狀態轉換採 120ms、180ms、240ms、320ms 四級時間，預設 easing 為 `cubic-bezier(0.16, 1, 0.3, 1)`。禁止 bounce 與 elastic motion。

### Page architecture and navigation

主要產品導覽固定五個分頁，順序不可因語系改變：

1. 英雄 / Champions
2. 增幅 / Augments，簡中標籤為海克斯
3. Draft
4. 小遊戲 / Game
5. 版本變動 / Patch Changes

Classic 是 header 內獨立的 pill link，可附 `NEW` badge，但不計入第六個主要分頁。Home、About、Privacy、Contact 是資訊頁，亦不加入產品 tab。尚未發布的 Articles / 專欄不計入頁數，也不能預留空白 tab。

Draft 內含 Draft 與 Draft Analysis 兩個 submode。Game 內含 Meta Pick 與 Augment Draft 兩個 submode。所有 submode 採同一組 tab 語意與 URL/state 規則，不另創一套視覺。

桌面 header 高度為 56px，字標在左、主要 tab 在中、theme、language、Classic 與其他 utilities 在右。行動版改為兩列，主要 tab 可水平捲動且 active item 必須被帶入可視區，不能把五個 tab 壓成無法閱讀的縮寫。

### Display modes

- **Theme:** Dark 是預設，Light 透過相同 semantic token 切換。Theme flip 在一個 tick 內停用 transition，避免中途顏色殘留。
- **Locale:** 繁中使用根路由，英文使用 `/en`，簡中使用 `/zh-CN`。五個主要 view、submode、搜尋索引與 accessible name 一起翻譯。
- **Viewport:** Desktop 使用固定 header 與 1320px rail；Mobile 使用兩列 header、可橫捲 nav、單欄或重排 detail。行動版重排資訊，不能只縮小桌面版。
- **Content density:** 榜單先顯示 summary grid，英雄 detail、次要證據與大型資料按需開啟。Wide view 可使用 side detail，narrow view 改用單欄 detail flow。
- **Motion:** Full motion 只用於狀態連續性；Reduced motion 停用 prism、shine、skeleton sweep 與非必要 view transition。

### Buttons

- **Primary:** Signal Gold 填滿、Ink on Gold 文字、8px 圓角、10px 14px padding，只用於當下唯一主要動作。
- **Quiet:** Raised surface、主要文字、1px hairline，用於 theme、language、close 與次要操作。
- **Hover:** 只提高亮度、邊界或輕微位移，不造成 layout shift。
- **Focus:** `focus-visible` 使用 2px accent outline 與足夠 offset，不能只重用 hover。
- **Disabled:** 降低對比並移除 pointer affordance，但文字仍需可讀。

### Chips and segmented controls

- **Style:** Pill shape、7px 10px padding、低彩度背景與 muted text。
- **Selected:** 使用 accent、role color 或明確底線，但依 chip 語意只能選一種 encoding。
- **Role filters:** 色彩只出現在 dot、細邊或小面積 selected state，label 始終保留。
- **Semantics:** Filter 使用 `aria-pressed`，tab 使用正確 tab 語意與 selected state，不能只靠 class 名稱。

### Panels and containers

- **Shape:** 主要 panel 使用 12px 圓角，內部 tile 或 control 使用 8px，大型 overlay 上限 16px。
- **Background:** 使用 canvas、surface、raised 三層，禁止無限制新增近似灰階。
- **Border:** 1px translucent hairline；禁止左側大於 1px 的裝飾色條。
- **Internal spacing:** 12px、16px、24px 為主要 group spacing，32px 用於大型 section separation。
- **Hierarchy:** 一個 panel 內可以有 section，不得再堆完整卡片 chrome。

### Search and inputs

- **Style:** 40px 高、8px 圓角、10px 12px padding、surface 背景、1px hairline。
- **Focus:** Border 切到 accent 並出現 2px focus ring，placeholder 不得變成唯一 label。
- **States:** Loading、empty、error 與 disabled 均保留同一高度和 layout，避免資料到達時跳動。
- **Search behavior:** Champion、augment 與跨語系 alias 可被索引，結果更新不應阻塞鍵盤輸入。

### Tier tiles and data rows

- **Tier group:** 依 OP 到 T5 固定排序，每組同時顯示 tier label、顏色與門檻或描述。
- **Champion tile:** 圖像是主要辨識物，tier color 只作 2px frame 或小型 pill；名稱與勝率分開對齊。
- **Selected tile:** 以 accent outline、brightness 與 detail state 共同表示，不以放大動畫移動 grid。
- **Data row:** 主要數值靠同一軸對齊，lift、games、patch 與不確定性按「答案、證據、可信度」排列。
- **OP treatment:** 稜彩只能出現在 OP frame 或 tier label，reduced motion 下保持靜態。

### Implementation boundary and packages

網站不是 React、Vue、Tailwind、Vite 或 Webpack 應用，也沒有 npm component library。Canonical source 是 Python renderer 加原生 HTML、CSS 與 JavaScript：

- `scripts/templates/site.css` 定義 token、layout、components、responsive 與 visual states。
- `scripts/templates/site.js` 管理 routing、theme、locale、filter、lazy detail、a11y state 與 View Transitions fallback。
- `scripts/tierlist_render.py` 定義 HTML topology、五個主要 view、locale shell、payload assembly 與 champion shards。
- `scripts/tierlist_engine.py` 定義 tier、Bayesian ranking、rarity percentile 與統計語意。
- Google Fonts 提供 Outfit、Noto Sans TC、Noto Serif TC；圖示使用 inline SVG，不引入 icon package。
- Data Dragon 與 CommunityDragon 提供英雄圖像與遊戲資料；瀏覽器端使用 Fetch API、ResizeObserver、View Transitions API、requestIdleCallback、localStorage 與 sessionStorage，所有進階 API 必須有 fallback。
- GitHub Pages 承載生成後 static site。Google Analytics、Cloudflare Analytics 與 AdSense 是可選服務，不是設計系統依賴。

首屏只載入 shell、summary payload 與必要索引。Champion-heavy detail 使用 per-champion shard，shared `site.js` 使用穩定檔名與 content-hash query，圖片 lazy-load，次要資料在 idle phase 載入。UI-only 修改應使用 shell-only build，不重算統計 payload。

**The Familiar-Control Rule.** 導覽、搜尋、tab、chip、button 與 detail panel 必須使用玩家熟悉的 affordance，禁止以新奇互動換取辨識成本。

**The Performance-is-Design Rule.** 如果首屏需要等待完整 champion detail 或次要 dataset，設計即不合格，不能用 skeleton 無限延長等待感。

## Do's and Don'ts

### Do:

- **Do** 使用 `Engineered Calm` 作為每個畫面的審核標準：資料密集、低彩度、層級清楚、互動焦點稀少。
- **Do** 維持五個主要產品分頁，Classic 與資訊頁保持次級入口。
- **Do** 讓 `aram` 保持 muted 500、`meta` 保持 primary 700，整體用小寫 Outfit 字標返回首頁。
- **Do** 用 1320px rail、56px header、24px page gutter，以及 8px、12px、16px radius family 建立一致節奏。
- **Do** 先顯示 tier 或建議，再顯示勝率、lift、games、patch 與限制。
- **Do** 使用 OP、T1、T2、T3、T4、T5 的文字、固定順序與色彩三重線索。
- **Do** 讓 dark、light、繁中、簡中、英文、desktop、mobile 與 reduced motion 共用相同資訊優先順序。
- **Do** 使用 semantic HTML、keyboard flow、`focus-visible`、ARIA state 與至少 40px 的主要輸入高度。
- **Do** 從 template、renderer 與 engine 修改設計，並把 `docs/` 視為可重建的發布產物。

### Don't:

- **Don't** 把 arammeta 做成高彩度電競海報、霓虹 cyberpunk 或遊戲商城。
- **Don't** 使用紫色奢華、玻璃擬態、巨大 glow 與裝飾性 gradient text。
- **Don't** 以大型 hero metric 和行銷口號取代可比較資料的 generic SaaS landing page。
- **Don't** 讓每個區塊都包成卡片，形成 card-in-card 的儀表板。
- **Don't** 用紅黃綠交通燈取代數值、tier 與不確定性的實際語意。
- **Don't** 為了動畫而移動內容、彈跳、旋轉或阻塞操作。
- **Don't** 把 `meta` 染成金色、拆開字標、加入電競斜體、描邊或 header logo icon。
- **Don't** 讓金色、tier 色或 role 色大面積填滿 panel；金色常態不超過畫面的 10%。
- **Don't** 使用超過 1px 的裝飾 side stripe、厚重短陰影或無功能的 backdrop blur。
- **Don't** 在 CSS、JavaScript 或文案內重新發明勝率門檻；統計語意只由 engine 輸出。
- **Don't** 新增第六個主要 tab，除非產品決策同時更新 `PRODUCT.md`、本文件、renderer、三語路由與測試。
- **Don't** 讓 mobile 只是縮小 desktop，也不要讓可水平捲動的 nav 隱藏目前 active item。
