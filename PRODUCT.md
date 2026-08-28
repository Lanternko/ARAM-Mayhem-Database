# PRODUCT.md

## Register

`product`

## Users

arammeta 服務需要在短時間內做判斷的 ARAM 與 Mayhem 玩家。他們可能在開局前研究版本、選角時比較英雄、遊戲中挑選增幅，或在賽後理解版本變化。主要語系是繁體中文，同時支援簡體中文與英文。

## Product Purpose

arammeta 把大量對局資料轉成可校準、可追溯的選角資訊，讓玩家先得到「現在該注意什麼」，再查看勝率、樣本數、版本、增益與限制。產品不是用視覺聲量替代證據，也不把相關性包裝成必勝答案。

核心成功條件：

- 玩家能在數秒內找到英雄、增幅或 Draft 建議。
- 每個重要結論都能看到足夠的信任線索，例如樣本數、版本、估計方法與資料更新時間。
- 同一套統計語意能跨英雄榜、增幅榜、Draft、Meta Pick 與版本變動頁維持一致。
- 新功能只有在不降低資料可信度、可用性與效能時才進入主要導覽。

## Brand Personality

精準、冷靜、熟悉遊戲語境、對不確定性誠實。介面應像一套經過校準的比賽儀器，資訊密度高但不吵雜，專業但不故作權威。

創意北極星是 `Engineered Calm`。在昏暗的遊戲環境中，深色炭灰介面保持安靜，少量金色只標示互動焦點；日間使用則由同一套語意 token 轉成明亮主題，而不是另一個品牌。

## Product Principles

1. **答案先於證據，證據緊跟答案。** 先呈現 tier、建議或變化，再提供勝率、lift、樣本、版本與限制。
2. **校準信心，不製造確定感。** 低樣本、跨版本與選擇偏差必須在排序或文案中被處理，不能只靠免責聲明補救。
3. **資料密集，但每一層都有目的。** 一個視圖只保留完成當前決策所需的控制項與指標，細節按需展開。
4. **熟悉優先。** 導覽、搜尋、篩選、tab、panel 與表格採玩家已熟悉的互動模式，避免為了新奇改變基本操作。
5. **效能也是可信度。** 首屏必須快速、狀態轉換可預期、次要資料延後載入，不能讓長時間空白削弱使用者對資料的信任。
6. **生成來源高於部署產物。** 設計與功能修改發生在 template、renderer 與 engine，`docs/` 只承載可重建的發布結果。

## Anti-references

arammeta 不應變成以下風格：

- 高彩度電競海報、霓虹 cyberpunk 或遊戲商城。
- 紫色奢華、玻璃擬態、巨大 glow 與裝飾性 gradient text。
- 以大型 hero metric 和行銷口號取代可比較資料的 generic SaaS landing page。
- 每個區塊都包成卡片，形成 card-in-card 的儀表板。
- 用紅黃綠交通燈取代數值、tier 與不確定性的實際語意。
- 為了動畫而移動內容、彈跳、旋轉或阻塞操作。

## Accessibility and Inclusion

- Dark 與 light theme 共享語意層級，不能只做反相。
- 所有核心操作支援鍵盤、清楚的 `focus-visible`、觸控尺寸與語意 HTML。
- 不以顏色作為唯一訊號，tier、正負變化與選取狀態同時使用文字、數值、形狀或位置。
- 尊重 `prefers-reduced-motion`，停用 prism、shine、skeleton sweep 與非必要轉場。
- 繁中、簡中與英文路由、標籤、搜尋索引及 accessible name 必須一起維護。
- 桌面與行動版維持相同資訊優先順序，行動版重新編排，不只是縮小桌面版。

## Decision Boundaries

- 排名、門檻、Bayesian shrinkage 與樣本下限由資料引擎決定，CSS 不得重新定義統計語意。
- `DESIGN.md` 定義視覺語言、資訊架構、元件與顯示模式；實作細節仍以 `scripts/templates/` 與 renderer 為準。
- command、部署與 live harness 流程不放在產品文件，分別由 `scripts/README.md`、`OPERATIONS.md` 與專用 skill 管理。
