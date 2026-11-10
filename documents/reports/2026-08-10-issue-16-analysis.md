# Issue #16 可行性驗證：玩家專精與動態海克斯資料

## 重現方式

以下指令以本機最新 Mayhem (`queue=2400`) SQLite 資料庫重建分析輸入，再產生報告：

```powershell
python scripts/extract_participants.py --db data/lcu/games.db --queue 2400 --out-dir data/ratings --rebuild --unordered
python scripts/extract_performance.py --db data/lcu/games.db --queue 2400 --out-dir data/ratings --rebuild --unordered
python scripts/analyze_player_champion_mastery.py --out-dir data/ratings/mastery_analysis_latest
python scripts/validate_augment_event_log.py data/overwolf/augment_events.jsonl
```

`--unordered` 是為了避免 SQLite 在系統暫存碟建立大型排序暫存檔；分析本身仍以 `created_ms` 做時間切分。

## 資料與隱私

- 14,858,640 個玩家英雄對局 slot、568,002 個本地 surrogate player、173 位英雄。
- 只有至少 10 場玩家對局、且至少 5 場該英雄對局的組合進入 193,456 筆專精統計。
- 輸出只使用本機整數 `pid`；不輸出 PUUID、Riot ID、summoner name，也不把 hash ID 放進公開網站 payload。

## 勝率與專精 backtest

以 `created_ms` 的 80/20 chronological holdout 評估（train 1,188,248 場、test 297,052 場；test 2,971,720 slots）：

| 模型 | Log loss | Brier |
|---|---:|---:|
| champion-only | 0.690802 | 0.248831 |
| champion + player | 0.691172 | 0.249016 |
| champion + player + mastery | 0.691159 | 0.249009 |
| champion + player + performance mastery | 0.691164 | 0.249011 |

目前結論是：玩家基線、玩家×英雄勝率 shrinkage、以及由 damage/gold per game 控制英雄後的 performance mastery，在這個全域對局勝率 holdout 沒有帶來增益，反而有極小退步。這不代表專精對個人推薦沒有價值；它表示在尚未加入隊友/對手組合、段位新鮮度與真正個人選角條件前，不應把它直接當成全站 Meta Pick 的勝率加成。

## 段位關聯

現有本地 rank snapshot 能對上的 502 位玩家；由於 rank DB 是舊 snapshot，這不是完整的當前段位覆蓋。Spearman 結果：

- champion concentration (Herfindahl) vs rank：rho 0.0188，p=0.675。
- mean mastery lift vs rank：rho -0.0267，p=0.659（275 位有足夠資料者）。
- mean performance mastery lift vs rank：rho 0.0584，p=0.334（275 位）。

目前沒有統計證據支持「段位越高，英雄專精集中度或專精勝率 lift 就越高」。下一步若要做個人化排序，應先刷新 rank label，再以段位分層校準，而不是把全體資料混成一個 bonus。

## 玩家英雄池的實際形狀

在至少 10 場對局的 353,667 位玩家中，觀察到的英雄數中位數為 23（P10=11、P90=47）；只看至少 50 場的 90,652 位玩家，中位數為 44（P10=34、P90=63）。但這是 ARAM 隨機分發英雄下的「被觀察到的池」，不是玩家自由選角池：第一名英雄的對局占比中位數只有 9.1%（至少 10 場）／6.8%（至少 50 場），只有 40 位玩家的第一名占比達 40% 以上。

因此答案是「玩家之間確實有不同的觀察分布」，但目前沒有看到很強的單一英雄專精；專精訊號應以玩家×英雄勝率與 performance residual 的 shrinkage 結果表達，不能只用玩過幾個英雄判定。

## 高低段位的角色差異（探索性）

將 rank snapshot 的 44 位 Diamond+ 玩家與 273 位 Gold 以下玩家比較，DDragon tags（可重疊，並非 ARAM 位置）得到的高低段位勝率差：Assassin +0.7pp、Fighter -0.1pp、Marksman -0.5pp、Support -1.3pp、Mage -1.8pp、Tank -2.4pp。這些差異很小，且 rank snapshot 過舊，不足以建立正式段位角色 bonus。

單英雄層級確實會看到探索性 outlier，例如 Lee Sin 約 +13.0pp、Gangplank +12.5pp、Ryze +10.2pp（高段位較高），Seraphine -21.2pp、Yasuo -14.3pp（高段位較低）；每個高段位英雄樣本約 30–73 場，尚未做多重比較校正，只能作為下一輪資料收集的候選，不可直接當成結論。

## 動態海克斯資料契約

新增 `src/aram_nn/augment_events.py` 與 `scripts/validate_augment_event_log.py`。canonical event 必須包含：

`schema_version`, `event_type` (`offer`/`picked`), `event_id`, `match_id`, `player_key`, `round_index`, `champion_id`, `patch`, `augment_ids`, `picked_augment_id`（picked 才需要）、`captured_at`, `source`。

目前 `data/overwolf/augment_events.jsonl` 有 159 筆（158 offer、1 picked），但 0 筆符合契約：缺少 match/round/champion/patch/player context，且 raw payload 只有海克斯名稱，無法安全 join 回一場對局。因此目前不能可靠估計「雙方英雄 × 已選海克斯順序」的條件勝率；先補齊事件 context 與穩定 event_id，才值得建立動態推薦模型。

## 產品判斷

這個 Issue 可行，但應拆成兩階段：

1. 先用 privacy-safe surrogate ID、段位快照與 champion-controlled performance 做個人化探索頁，展示樣本數、信賴區間與高上限英雄，不改寫全站 aggregate Meta Pick。
2. 在 LCU/Overwolf bridge 補齊 offer/picked context 後，才做海克斯序列的 hierarchical/時間切分模型；資料不足時回退到英雄×海克斯的 shrinkage prior。

機器可讀原始結果位於 `data/ratings/mastery_analysis_latest/report.json`。

## 更正：Issue #16 的「段位」應使用自製 ELO

前面的 rank snapshot 段落使用了 Riot Solo Queue rank；那不是 Issue #16 所指的段位，不能作為本題結論。正確的指標是 `scripts/build_player_ratings.py` 產生的 Glicko-1 player rating（我們口語稱 ELO），以及由玩家 rating 聚合出的 lobby `avg_rating`。

用最新資料以時間前 80% 建立 ELO、後 20% 做 future holdout：

- train 1,191,609 場、test 297,902 場。
- 至少 10 場 train 對局的玩家，其 ELO P25=1457、P50=1511、P75=1560。
- 高 ELO（P75 以上）test 勝率 49.96%，低 ELO（P25 以下）50.42%；沒有整體勝率差。
- DDragon tag 的高低 ELO 角色差距介於約 -0.1pp 到 -1.1pp，沒有穩定的高 ELO 角色 bonus。
- 單英雄的探索性最大差異約 +5.3pp（Bard）到 -5.6pp（Pantheon），遠小於把同一批資料用 final ELO 回看時產生的假性大差異。

這個 future holdout 才是目前對「高 ELO 是否搭配某些英雄」的有效回答。原始 Glicko script 的 split-half reliability 只有 r≈0.02，表示現有自製 ELO 在玩家大量只出現少數對局、且玩家圖譜隨時間變動的條件下仍不夠穩定；在改善 rating 穩定性前，不應用它硬切正式段位 bonus。

## 更正：目前玩家場數是 crawler coverage，不是玩家完整最近 20 場

最新 crawler 實際命令使用 `--history-window 20 --games-per-player 4`。因此一次處理某玩家時，雖然 LCU 歷史清單可見約 20 場，但只會取其中最新 4 場；其餘對局只有在其他玩家的 crawl 間接帶回時才可能進入 DB。`players.games` 是「我們已捕獲且在 participants payload 看見該玩家的 Mayhem 場數」，不是該玩家可取得的完整最近 20 場。

最新 coverage 分布：568,002 位玩家中，56,119 位只有 1 場；中位數 16 場；只有 249,591 位（43.94%）達到至少 20 場。這解釋了為何不能把中位數 16 解讀成「玩家只打 16 場 ARAM」。若要研究穩定 ELO，應以 `games-per-player=20`（或不設 cap）回補既有 `crawl_seen` 玩家，再以至少 20 場的 cohort 重算。

ELO 的正確驗證不是比較高 ELO 玩家自己的總勝率，而是比較同一場的兩隊平均 rating gap：近似地，100 rating gap 的預期強隊勝率約 64%，200 gap 約 76%；同 ELO 對局接近 50% 是正常的。`game_quality` 目前使用 final rating，回看同一批對局會有循環偏差；正式校準必須使用每場開打前的 rating snapshot。

本輪已將 snowball、snowball-workers、OPGG auto-refresh、watchdog 與 macOS launcher 的預設 `games-per-player` 改為 `0` 的 adaptive 模式：先看最近 4 筆歷史，0 場 Mayhem 不展開、1–2 場只展開近期 probe、3–4 場才展開整個 history window。既有已啟動、明確帶 `--games-per-player 4` 的 worker 不會自動改變，需在安全時機重啟才會套用。每場十位玩家 ID 的 private payload 已經存在，最新抽取確認 1,485,864 個 roster 產生 14,858,640 個 slots，正好每場 10 位；這部分不需要再新增欄位。
