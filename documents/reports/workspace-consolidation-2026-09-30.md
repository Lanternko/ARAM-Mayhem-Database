# 2026-09-30 工作區整合與發布

以最新 origin/main 為基底，避免舊 worktree 回退已發布的英雄頁、頭像與 Classic 修正。

## 保留並發布
- 三語回饋入口：英雄、增幅與版本變動頁末提示，頁尾回饋按鈕。
- 推薦增幅池使用低干擾標籤；最佳增幅卡片移除重複 verdict badge。
- Publisher／model refresh 分別使用有界記憶體門檻；等待資源不占共用鎖，取得鎖後再檢查。
- Arrow 批次匯出、team-column source row index 修正與相關測試。
- Riot launcher 423 stale lock recovery；操作、發布與 worktree 文件更新。
- 恢復被過寬 models/ ignore 排除的共用模型原始碼，模型權重仍不提交。

## 清理與封存
- 已整合或被新版取代的英雄頁、Classic、search、feedback 與 pool hover 工作副本退役。
- 舊 augment training、single-writer、player-history 與 resource tuning 工作副本封存；不把舊整包 diff 或探索性模型設為 production default。
- 保留 primary checkout、正在使用的 API runtime 與自動 publisher 的暫存 checkout。
- 清理前保存所有 Git refs bundle、各 worktree 的 HEAD／status、staged／unstaged binary patches，以及非 ignored untracked 檔案。封存位於 sibling workspace 的 backups/consolidate-20260930，含私有本機內容，不上傳。

## 驗證與發布範圍
- Frontend shell lane；公開 tier-list JSON 與 champion shards 逐位元保留，沿用 patch 16.19 快照。
- 驗證 memory guard、batch loader、model preflight、publisher、Classic scheduler、adaptive history、Draft payload、shell snapshot preservation 與 feedback entry points。
- Desktop／390px mobile 的 dark／light 畫面、鍵盤焦點及 console 檢查。
- 僅發布靜態介面與 repository source；不重啟 crawler、watchdog、API 或 model daemon，不宣稱既有程序已重新載入 code。
