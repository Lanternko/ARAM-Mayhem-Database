# Documents Index

`documents/` 保存可閱讀的研究輸出、架構說明與 audit；它不是網站 `docs/` 生成目錄。大型簡報 workspace、node_modules、圖片與匯出檔仍是 local-only，不納入主要 repository 文件圖。

## Active references

- `architecture/backend-frontend.md` — 公開站、private collector、optional backend 與 privacy boundary。
- `personal-data-audit.md` — repository 個資／secret audit 快照。
- `reports/README.md` — 報告版本、final/draft/evidence 狀態。

## Local workspaces

- `open-slide-ethereal-items/`
- `open-slide-final-project/`

這兩個目錄是各自有 `AGENTS.md`、dependencies 與 build contract 的獨立 Open Slide workspace，只是暫存在 `documents/`；不屬於根專案文件階層，也不應被根專案的 Markdown audit 遞迴納入。

新增文件時要從本索引或對應子目錄 README 連入。若內容只是歷史快照，放到 `notes/archive/` 或在標題下方明確標示 archived/superseded。
