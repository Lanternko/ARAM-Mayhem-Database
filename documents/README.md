# Documents Index

`documents/` 保存可閱讀的研究輸出、架構說明、指南、報告與歷史文件；它不是網站 `docs/` 生成目錄。這是 `documents/` 唯一的索引，子目錄不再各自放 README。大型簡報 workspace、node_modules、圖片與匯出檔仍是 local-only，不納入主要 repository 文件圖。

Current owners：產品決策在 [`../PRODUCT.md`](../PRODUCT.md)，網站設計在 [`../DESIGN.md`](../DESIGN.md)，模型與資料決策在 [`../MODEL.md`](../MODEL.md)，live operation 在 [`../OPERATIONS.md`](../OPERATIONS.md)。放置、命名與歸檔規則見 [`../runbooks/repo-hygiene.md`](../runbooks/repo-hygiene.md)。

新增、搬移或歸檔文件時，在同一個 commit 更新本索引；`tests/test_repo_layout.py` 會檢查每個文件都有列在這裡。

## Architecture

- [`architecture/backend-frontend.md`](architecture/backend-frontend.md) — 公開站、private collector、optional backend 與 privacy boundary。

## Guides

- [`guides/experiments-and-data.md`](guides/experiments-and-data.md) — 實驗 manifest、變因控制、evaluation 與 promotion gate 的執行模板。

## Reports

依日期新到舊。報告內的數字只適用於其標示的 dataset、patch 與日期；現行 modeling policy 見 [`../MODEL.md`](../MODEL.md)，網站設計見 [`../DESIGN.md`](../DESIGN.md)。

- [`reports/2026-11-10-repo-reorganization.md`](reports/2026-11-10-repo-reorganization.md) — 一次性 repository 整理 spec 與執行紀錄；長期規則在 [`../runbooks/repo-hygiene.md`](../runbooks/repo-hygiene.md)。
- [`reports/2026-10-15-crawler-throughput.md`](reports/2026-10-15-crawler-throughput.md) — Crawler 降速診斷與修正（單一 SQLite writer 與 LCU 探測重啟行為）。
- [`reports/2026-10-09-recommender-release.md`](reports/2026-10-09-recommender-release.md) — Windows 自動更新版 recommender 的發布與驗證結果。
- [`reports/2026-10-09-augment-encounter-bound.md`](reports/2026-10-09-augment-encounter-bound.md) — 增幅整場出現上界的模型與決策範圍。
- [`reports/2026-10-03-augment-weight-expansion.md`](reports/2026-10-03-augment-weight-expansion.md) — 增幅池權重是否等於發放機率倍數（16.18＋16.19，約 2 倍量級；歷史存檔）。
- [`reports/2026-10-03-search-discovery.md`](reports/2026-10-03-search-discovery.md) — arammeta 搜尋曝光與收錄基礎。
- [`reports/2026-09-30-workspace-consolidation.md`](reports/2026-09-30-workspace-consolidation.md) — 2026-09-30 工作區整合與發布紀錄；內文描述當時狀態，路徑可能已過期。
- [`reports/2026-08-10-issue-16-analysis.md`](reports/2026-08-10-issue-16-analysis.md) — Issue #16 的研究分析（玩家專精與動態海克斯資料）；結論需和 `MODEL.md` 的現行 policy 一起解讀。
- [`reports/2026-06-14-final-project-report.md`](reports/2026-06-14-final-project-report.md) — 現行 final project report；引用當時 dataset 與結果時以此版為準。
- [`reports/2026-06-11-composition-analysis-handoff.md`](reports/2026-06-11-composition-analysis-handoff.md) — composition 分析 handoff；保留實驗證據與限制，不是現行 policy owner。
- [`reports/2026-05-15-personal-data-audit.md`](reports/2026-05-15-personal-data-audit.md) — repository 個資／secret audit 快照（skill 格式的審計流程說明）。

## Archive

本區只保存歷史背景、被否決的方向與可追溯快照；沒有任何檔案可以自稱 current canonical。被新版取代的報告在檔頭標示 `Superseded by`。

- [`archive/PLAN-2026-05-12.md`](archive/PLAN-2026-05-12.md) — 原根目錄 `PLAN.md`：2026-05-12 的 historical plan（standard ARAM、queue 450）；現行模型決策只看 `MODEL.md`。
- [`archive/MODEL_NOTES-2026-05-20.md`](archive/MODEL_NOTES-2026-05-20.md) — 2026-05 模型方向快照（含併入的舊 `MODEL_NOTES.md`）；已被 `MODEL.md` 取代。
- [`archive/PRODUCT.md`](archive/PRODUCT.md) — 舊產品敘事；已被根目錄 `PRODUCT.md` 取代。
- [`archive/DESIGN.md`](archive/DESIGN.md) — 舊 design system；已被根目錄 `DESIGN.md` 取代。
- [`archive/TODO.md`](archive/TODO.md) — 2026-05 dataset 與實驗 backlog 快照；不是現行工作清單。
- [`archive/article-cover-banners.md`](archive/article-cover-banners.md) — 尚未發布 Articles feature 的 cover convention。
- [`archive/design-experiments/z7k-washoku.md`](archive/design-experiments/z7k-washoku.md) — 和色 Zen preview 的設計實驗。
- [`archive/2026-05-31-final-project-text-report.md`](archive/2026-05-31-final-project-text-report.md) — 被 2026-06-14 final report 取代的文字草稿。
- [`archive/2026-06-28-site-redesign-handoff.md`](archive/2026-06-28-site-redesign-handoff.md) — 歷史 redesign handoff；現行設計以根目錄 `DESIGN.md` 為準。

## Local workspaces

- `open-slide-ethereal-items/`
- `open-slide-final-project/`

這兩個目錄是各自有 `AGENTS.md`、dependencies 與 build contract 的獨立 Open Slide workspace，只是暫存在 `documents/`；不屬於根專案文件階層，也不應被根專案的 Markdown audit 遞迴納入。
