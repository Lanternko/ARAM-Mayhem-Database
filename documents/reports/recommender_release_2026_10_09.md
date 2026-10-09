# Windows 自動更新版驗證 — 2026-10-09

## 發布結果

- [Release 2026.10.09.2](https://github.com/Lanternko/ARAM-Mayhem-Database/releases/tag/recommender-v2026.10.09.2) 是 latest stable。提供單檔 EXE、便利 ZIP、純 JSON 資料 ZIP 與 SHA-256 manifest。
- [來源 PR #36](https://github.com/Lanternko/ARAM-Mayhem-Database/pull/36)。發布 EXE 的 source commit 是 `f8113b63950a2739a1ddd7e270d492cc58710518`；source 不需先整合進 live harness 即可由 release tag 重現。
- 對象：Windows 10/11 x64。使用者不用 Python、GitHub token 或管理員安裝。
- 每次啟動檢查已發布的最新程式與相容資料；下載支援續傳、SHA-256、架構與 schema 驗證。Immutable installations + atomic pointer 保留完整舊資料，並支援舊 EXE 啟動快取中的新 EXE。

## 資料範圍

Mayhem queue 2400、TW；current patch 16.20，模型採 16.18/16.19/16.20 pooled + recency weighting，半衰期 7 日。既有模型 refit rows 1,068,222；資料截止自相同 parquet 的 `game_creation_ms` 最大值取得：2026-10-09 05:26:28.219 +08:00。這次沒有改模型、split 或推薦排序。

公開 artifact 約 106 KB，只有 numeric model、英雄 vocabulary、role synergy、校準、英雄 alias、champion win-rate 聚合與 scope metadata。精確白名單 8 個 JSON；沒有 pickle、PUUID、Riot ID、summoner name、原始對局或 SQLite。完整 EXE 約 36.5 MB，ZIP 約 36.2 MB。

## 已驗證

- Updater、publisher、原 recommender sharing 與 model refresh preflight 相關測試通過，涵蓋續傳、中断重試、錯誤雜湊、錯誤 Range、架構/schema/queue、不完整資料、路径穿越、重複檔名、損壞快取、offline fallback、跨程序 lock、禁止 downgrade，以及新 data schema 不阻止 app upgrade。
- 從可信本機 pickle 轉 JSON：相同 100 組 seed=42 隊伍的勝率差異最大值 0.0。
- Windows 11 x64，中文／空白目錄，移除 Python PATH/PYTHONHOME/PYTHONPATH：EXE bundled Tk 8.6.15 與 173 位英雄的模型推論通過。
- 本機 public channel：首次 EXE 啟動自動下載最新資料；再次啟動重用同一快取，顯示「已是最新資料」。
- [Hosted Windows smoke](https://github.com/Lanternko/ARAM-Mayhem-Database/actions/runs/37877714469)：windows-2022 與 windows-2025 都通過 public download + digest、無 Python 路徑的 bundled Tk/inference、online data update，以及實際 2026.10.09.1 → 2026.10.09.2 EXE 自動下載、安裝與啟動。第一次 windows-2025 開始早於 Release 公開而收到 404，公開後重跑通過。

## 邊界與後續操作

- Hosted runners 是乾淨 Windows Server 相容性證據；尚未實测 Windows 10 玩家硬體、所有 LoL 安裝位置、LCU 權限與 SmartScreen/MOTW 情境。EXE 尚未簽章。
- 額外以無服務代理模擬 EXE 離線的指令被自動核准審查拒絕，未繞過；offline fallback 使用故障注入測試確認。
- 2026-05-26 舊 EXE 沒有 updater，使用者必須下載新版一次。
- 本次已發布最新版資料，但既有 live model refresher 沒有重啟或變更 argv。新 source 支援 opt-in `--desktop-release-tag recommender-v2026.10.09.2 --desktop-region TW`；只有部署 source、接入 production child argv 並驗證下一次 publish log 後，才能說未來本機 model refresh 會自動發布。使用者端只能下載已公開版本。
