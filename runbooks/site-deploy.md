# Site Publish Runbook

## Mental model

正式站是 GitHub Pages 的 `main` branch `/docs`。它是靜態檔案發布，不是常駐 website process：deploy 只會 build、commit、push，**不會重啟網站、crawler、watchdog、API、tunnel 或 League client**。不要把「full data build」說成「restart the full website」。

Source of truth 是 `scripts/templates/`、renderer／builder 與 private data；`docs/` 是生成產物。完整 data artifact allowlist 只由 `src/aram_nn/site/static_publish.py::DEFAULT_DOC_PATHS` 擁有，不在文件或 skill 手抄第二份。

## Intent contract — do not ask twice

| 使用者表達 | 行為 | 是否再確認 |
|---|---|---|
| deploy、publish、ship、上線、更新 live site | 選正確 lane，完成 build→驗證→commit→push→live check | 不再問「確定要部署嗎」 |
| frontend-only deploy、CSS／JS／copy 上線 | 固定走 frontend shell lane | 不改成 full data build，也不再確認 |
| refresh data、更新勝率／patch／tier list | 固定走 routine data lane | 不再確認 |
| fix／change／preview，但沒有 live／deploy 語意 | 修改並 local preview／驗證 | 不 push |

只有 lane 無法從需求和 diff 判斷、會夾帶 unrelated WIP、需要額外服務重啟、test／build 失敗或出現 merge conflict 時才停下詢問。若 agent 能從修改範圍判斷，就直接執行，不把可自行回答的問題丟回使用者。

## Choose one lane

| Lane | 何時用 | 公開資料 | 計算量 | Runtime restart |
|---|---|---|---|---|
| 1. Routine data publish | 新 games、勝率、patch、tier／augment／radar／axes 更新 | 更新 | 完整 data build | 無 |
| 2. Frontend shell publish | CSS、JS、copy、layout、HTML shell、SEO metadata | 沿用目前 payload | shell-only，秒級 | 無 |
| 3. Generator／schema publish | public JSON contract、統計邏輯、renderer data assembly 改變 | 可能更新 | tests 後完整 data build | 無 |

Generic「deploy these changes」先看 diff：只有 template／shell UI 就用 lane 2；資料或 payload contract 改動用 lane 1／3。不要因為同一網站就一律跑 full publisher。

Repository-only integration 不是第四條 site lane。若 diff 只有 `AGENTS.md`、runbooks、skill adapters、tests 或內部 publish tooling，而且不改變 live shell、public payload 或 generated artifact contract，就依 `runbooks/git-workflow.md` 完成 tests→commit→push；不要為了把 source 上線而假跑 shell build 或 full data build。回報必須明示 live UI 與資料均未重建。

## Lane 1 — routine data publish

使用者明確要求立即更新 live data 時，直接執行：

```powershell
python scripts/publish_static_site.py --once --force --patch-prefix auto
```

`--force` 只略過 growth threshold；不略過 build、privacy、allowlist、Git conflict 或 verification。Publisher 會從最新 `origin/main` 建 detached disposable worktree，重算 split payload 與相依 artifacts，只 commit `DEFAULT_DOC_PATHS`，再 push `HEAD:main`。

這條 lane 不會帶入 development worktree 的 source WIP，也不會重啟任何 runtime。自動 watch 才使用 10% growth／12 小時門檻；使用者已明確要求 deploy 時，不先跑 `--check-only` 再回來詢問。

## Lane 2 — frontend shell publish

適用於 `scripts/templates/site.css`、`site.js`、copy／layout 或只影響 HTML shell 的 renderer change。Production-safe shell build 是：

```powershell
python scripts/build_tier_list.py --shell-only --site-url "https://arammeta.com/"
```

Production URL 會自動補 canonical split payload、Meta Pick API 與公開 analytics token。Shell-only 讀取現有 `docs/api/tier-list.json`，重建 HTML route shells 與 `docs/assets/site.js`，跳過勝率、augment、item／affinity 等完整資料計算；它不把 collector 新增 rows 偷渡進公開 snapshot。

依 `DESIGN.md` 做受影響 viewport、theme、keyboard／touch 與 console QA。若使用者要求 deploy，review 後 commit 明確的 template／renderer source 與相依 shell outputs，再依 `runbooks/git-workflow.md` 整合並 push；這個 deploy scope 已經是 main integration 的授權，不要再問一次。

Frontend lane 不得 stage `docs/api/tier-list.json`、champion shards、radar 或 axes。若 shell build 讓 data artifacts 出現實質 diff，停止並調查；不要擴大成 full data deploy 來掩蓋 scope drift。

## Lane 3 — generator or schema publish

只有 public payload schema、統計計算、sharding、artifact dependency 或 builder contract 改變才走這條 lane。先 review source、跑相關 tests，再整合 source；接著用 lane 1 做完整 atomic build，使 generated artifacts 與新 code 同版。

純 CSS／copy／layout 不屬於 lane 3。不要因為碰到 `tierlist_render.py` 就自動判成 full build；看改動是否改變 data contract，shell-only 能完整反映就仍用 lane 2。

## Automation boundary

Watchdog 的 `publish_static_site.py --watch` 只屬於 routine data lane：依 filtered games 成長 10% 或最長 12 小時評估。Frontend change 不等待 data growth，也不靠 watcher 猜測 source scope。

Data publisher 的 Git contract 維持 isolated：fetch latest remote、detached build、push 前再檢查 remote；remote 前進就丟棄舊 build 並重建，不 merge stale artifacts、不 force push。Publisher 只清理自己建立的 temporary worktree，不整理 human／agent task worktree。

## Stop conditions

- Build／tests／visual QA 失敗：修正或回報 evidence；不以 full deploy 繞過。
- Diff 含 DB、cache、logs、credentials、player identifiers 或 unrelated WIP：停止，不 stage。
- Remote／merge conflict：保留 evidence，依 `runbooks/git-workflow.md` 處理；不 force push。
- Shell lane 缺現有 payload：需要一次 routine data build；這是技術 blocker，不是重新詢問 deploy intent。
- Backend API code 真的需要 service restart：不屬於 Pages deploy，先切到 `OPERATIONS.md` 的 service owner，且只在使用者 scope 涵蓋 backend 時執行。

## Verification by lane

Routine data：確認 live payload patch／timestamp／row count、主要 JSON 200、首頁／英雄／augment／draft／game 可用，commit 只有 atomic allowlist。

Frontend shell：確認 live CSS／JS／HTML change、canonical／OG／analytics metadata、desktop/mobile 與 console；確認公開 payload version 與 data timestamp 沒有因 UI ship 改變。

Pages／CDN 可能短暫延遲，但這是 artifact propagation，不是 website restart。回報時明示本輪是「data publish」或「frontend shell publish」，並說明哪些資料有／沒有重算。
