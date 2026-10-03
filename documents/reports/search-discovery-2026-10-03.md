# arammeta 搜尋曝光與收錄基礎

日期：2026-10-03（Asia/Taipei）。範圍：arammeta.com；本次為 frontend shell publish，沿用既有台服 Mayhem queue 2400、patch 16.19（產品顯示 26.19）聚合資料，未重算統計或修改 `docs/api/`。

## 已確認的問題與處理

- `/sitemap.xml`、`/robots.txt` 原為 404，Search Console 無已提交 Sitemap。加入 canonical HTML 自動產生清單，包含 542 個可收錄網址：173 位英雄 × 三語，以及 23 個主要／資訊頁。每輪 full／shell build 都更新，atomic publisher allowlist 同步納入。
- Google 對 `/champions/ahri/` 的既有索引結果是「頁面會重新導向」，未收錄，canonical 為首頁（最後檢索 2026-09-25）。英雄網址原本以 JavaScript／meta refresh 跳回首頁。本次改為直接提供英雄 snapshot 與自己的 canonical，再原地載入共用互動 shell，移除重新導向。
- 分頁的搜尋描述原繼承首頁。本次按 route 用途更新 description，與既有 OG 描述一致。
- WebSite `alternateName` 原為一整段 SEO 標題。本次改為簡短品牌別名 AramMeta／ARAM Meta；主名稱維持 arammeta。

## Search Console 觀測

- 首頁已收錄，先前重新建立索引要求已成功加入優先檢索佇列。
- `/augments/pools/` 已收錄，Google 選用受檢測網址作 canonical，爬取與索引均允許；最後檢索 2026-10-03 17:00:29。
- `/en/` 已收錄，Google 選用受檢測網址作 canonical；最後檢索 2026-10-01 15:20:54。
- 搜尋成效報表仍顯示「資料處理中，請等約一天後再返回查看」，無查詢資料可分析。不可把此狀態解讀成零曝光或以猜測補關鍵字。

## 驗證與設計邊界

- 36 項相關測試通過；檢查 Sitemap 每個 canonical 都有實際 HTML 且允許收錄，2,160 個 locale alternate 目標皆存在。
- 公開 payload 與 champion data shards 保持逐位元不變。
- 直接開啟繁中／英文／簡中阿璃頁可載入既有互動詳情，canonical 保留在對應英雄網址；深淺主題、手機 390px 無水平溢出、詳情增幅池與鍵盤返回英雄榜驗證完成。
- 英雄首份 HTML 約 4.6 KB，共用互動 body 約 161 KB；避免複製 519 份完整 SPA。共用 body 標記 noindex，私有工具、404、轉址與非 canonical 別名不進 Sitemap。
- 手機尺寸的 Chrome 截圖介面逾時；手機檢查以可存取樹、DOM 尺寸與實際互動為證據，未將逾時截圖視為視覺驗收。

## 正式站與 Google 提交證據

- frontend shell commit `113e212315db7d3fd0cb4015524bf00ff53ecfa0` 已部署，GitHub Pages run `37116885881` 成功。正式 Sitemap、robots、三語阿璃 HTML 與兩個共用 assets 均 HTTP 200；Sitemap 解析為 542 個 URL。
- Google 對阿璃頁的即時測試（2026-10-03 18:40）顯示擷取成功、允許檢索與編入索引，宣告 canonical 為 `https://arammeta.com/champions/ahri/`；重新建立索引要求已加入優先檢索佇列。這是可收錄證據，尚不代表已收錄。
- Sitemap 提交已受理，但第一輪報表顯示「無法擷取」。依 Google 官方診斷流程檢查 Sitemap 的即時測試（18:43），顯示擷取成功與允許檢索；未要求把 XML 本身建立搜尋索引。已在成功擷取後重新提交，報表仍顯示「無法擷取」與 0 discovered pages；不可宣稱已處理成功。Google 官方說明會在後續幾天重試，尚待外部處理結果。人工判決處罰報表顯示「未偵測到任何問題」。
- 截圖證據保存於 primary checkout 的 `outputs/design/google-sitemap-submitted.png`、`google-sitemap-live-test.png`、`google-hero-live-test.png`、`google-hero-index-request.png`、`google-sitemap-resubmitted.png`。

## 後續以證據決策

Search Console 資料可用後，以查詢／landing page 的曝光、點擊、CTR 和平均排名找缺口；優先處理有曝光但低 CTR 的頁面、未收錄英雄與 canonical 錯配。不要僅因總點擊增加就宣稱 SEO 改動有效，需考慮時間範圍、版本、內容更新與 Threads 導流。Sitemap 提交與 live test 成功代表可發現與可編入索引，不代表所有頁面已收錄，也不保證排名。

官方依據：[Sitemap 擷取診斷](https://support.google.com/webmasters/answer/7451001?hl=en)、[Sitemap](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap)、[網站名稱](https://developers.google.com/search/docs/appearance/site-names)、[多語標記](https://developers.google.com/search/docs/specialty/international/localized-versions)。
