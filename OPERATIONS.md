# OPERATIONS — harness 總圖

這份文件是「這台機器上有什麼東西一直在跑」的單一入口。細節/故障排除見 CLAUDE.md「Stall Playbook」;這裡只畫鏈路、給重建指令。

## 1. 自動化鏈（Windows 排程任務）

```
排程任務 MayhemLCUWatchdogKeepalive（每分鐘觸發,State=Ready）
  → wscript //B //NoLogo scripts/watchdog_keepalive_hidden.vbs   (無視窗)
    → scripts/watchdog_keepalive.ps1                              (檢查/啟動)
      → pythonw scripts/mayhem_lcu_watchdog.py --workers N ...
```

`mayhem_lcu_watchdog.py` 一次背起三個職責（各自獨立 watch loop，同一顆排程任務養活全部）：

1. **Collector keepalive** — 監控 client 記憶體,超過門檻自動 restart/degrade（`--client-restart-mb` / `--degrade-client-mb`）,底層跑 `lcu_collector.py snowball`。
2. **Tier-list 自動發布** — 資料量 +10% 就觸發一次 `publish_static_site.py --watch`（`--growth-ratio 0.10`）。
3. **Recommender model 自動 refresh** — 每個 patch 資料量 +25% 觸發 `refresh_recommender_models.py --watch`（`--growth-ratio 0.25`,`--min-current-games 15000`）。

以上三支現在都是**背景常駐行程**,不是排程任務直接啟動 —— `mayhem_lcu_watchdog.py` 本身用 `--watch` 把它們當子行程管理。確認活著：`Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'"` 篩 `mayhem_lcu_watchdog.py` / `publish_static_site.py` / `refresh_recommender_models.py`。

## 2. 發布鏈（tier-list 網站）

- `src/aram_nn/site/static_publish_cli.py`（經 `publish_static_site.py` 呼叫）:
  - `--patch-prefix auto` **每個 watch cycle 重新解析**目前最新 patch,不需要重啟就會自動換版（2026-06-29 修復,見下方連結）。
  - **canonical URL 是 startup-bound** —— 若要改 `DEFAULT_SITE_URL`,必須重啟 publisher（kill 現有行程,watchdog 60 秒內重生;或手動重啟）。
  - `DEFAULT_DOC_PATHS`（`docs/index.html` 等）任一檔案在 working tree 是 dirty 狀態,publisher 會拒絕覆寫並 **crash-loop**。發布前確保這些檔案乾淨或已提交。
  - 強制立即發布：`python scripts/publish_static_site.py --once --force`
- 部署細節 / 手動流程見 `.claude/skills/deploy-tier-list/SKILL.md`。

## 3. 同步鏈（本機 DB → 公開 backend）

- `scripts/sync_site_backend.py --watch` — 每新增 10,000 筆 filtered local games 才推一次;首次灌資料用 `--force`。
- Backend schema／PII 過濾規則見 `src/aram_nn/site/db.py`（拒絕含 puuid / summonerName / riotId / UUID-like PUUID 的 `participants_json`）。

## 4. Recommender（本機 GUI，非站台）

- 啟動方式：桌面捷徑「**ARAM Recommender (source)**」→ `pythonw scripts/recommend_gui.py`,**永遠跑 source**,程式碼改完下次開啟就是新版。
- **永不 rebuild exe**（`scripts/build_recommender_exe.py` 產的 `dist/ARAMRecommender.exe` 路線已棄用,2026-07-06 已刪除 `dist/`+`build/`）。
- watchdog 只 refresh `models/composition_lr_pooled_recency_7d/` 底下的資料/權重,不碰 recommender 本身的程式碼或啟動方式。

## 5. Log 位置

- 常駐服務目前 log：`logs/`（`mayhem-lcu-watchdog*.log`、`opgg-autorefresh.*`）,已 gitignore。
- 舊手動 run 遺留的 log（2026-05~06,`data/lcu/*.log`,約 40 個）：**2026-07-06 整理時尚未搬**——因為 collector/watchdog 當時正在跑,依鐵律「collector 跑時不動 data/lcu/ 任何檔案」延後。下次 collector 閒置時執行：
  ```powershell
  Move-Item data/lcu/*.log logs/archive/lcu-runs/
  ```
  （先 `mkdir logs/archive/lcu-runs` 若不存在;`games.db`/`-wal`/`-shm` 三件套絕對不要移動。）

## 6. 故障排除

不重複內容 —— 直接看 `CLAUDE.md` 的「Stall Playbook」章節（seed family 判斷、backoff 陷阱、LCU 憑證假過期等）。

## 7. 排程任務清單與重建

目前只有一個排程任務：

| 名稱 | 觸發 | Action |
|---|---|---|
| `MayhemLCUWatchdogKeepalive` | 每分鐘 | `wscript.exe //B //NoLogo scripts\watchdog_keepalive_hidden.vbs` |

XML 定義已備份到 `notes/task-definitions/MayhemLCUWatchdogKeepalive.xml`（`schtasks /query /tn MayhemLCUWatchdogKeepalive /xml` 匯出,2026-07-06）。重灌機器或誤刪後還原：

```powershell
schtasks /create /tn MayhemLCUWatchdogKeepalive /xml notes\task-definitions\MayhemLCUWatchdogKeepalive.xml
```

## 相關連結

- 整體專案慣例 / NEVER 規則：`CLAUDE.md`
- 深度技術決策：`PLAN.md`
- 部署 SOP：`.claude/skills/deploy-tier-list/SKILL.md`
- 英雄雷達重建 SOP：`.claude/skills/champion-radar/SKILL.md`
