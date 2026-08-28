# Archived Design Experiment: 和色 Zen

> **Experimental / superseded:** 這是 `/p/z7k-washoku/` preview 的歷史設計假說，不是 production design owner。現行品牌、配色、排版與 CSS 規範見 `../../../DESIGN.md`。

## Philosophy

色庫取自 NIPPON COLORS。大面積背景與面板使用墨、藍墨茶、消炭、白練、銀鼠等低彩度色；高彩度只用於需要注意的功能狀態。職業色以近似飽和度平行轉色相，勝率與 tier 以少量色相改變明度／彩度，不使用無語意漸層或大面積光暈。

## Historical palette

| 用途 | 色名 | Hex |
| --- | --- | --- |
| 頁面底 | 墨 SUMI | `#1C1C1C` |
| 面板 | 藍墨茶 AISUMICHA | `#373C38` |
| 次面 | 消炭 KESHIZUMI | `#434343` |
| 主字 | 白練 SHIRONERI | `#FCFAF2` |
| 次字 | 銀鼠 GINNEZUMI | `#91989F` |
| Accent | 今様 IMAYOH | `#D05A6E` |
| Positive | 常磐 TOKIWA | `#1B813E` |
| Negative | 赤紅 AKABENI | `#CB4042` |

歷史 filter 強度階以「峰值白底深字、高值色底白字、中低值透明底降飽和」區分。若要復用，只能把它當研究輸入，重新對照 `DESIGN.md` 的語意 token、contrast 與元件狀態。
