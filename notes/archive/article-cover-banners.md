# Archived Article Cover Banner Convention

> **Archived:** Articles feature 目前未發布。本檔由舊的 `docs/assets/covers/README.md` 移入，避免手寫規範混在生成用 `docs/`。若重新啟動 feature，先以根目錄 `DESIGN.md` 與現行 renderer 架構重設 contract。

歷史 convention：專欄封面採 16:9（例如 1920×1080 或 1600×900），檔名為 `<article-id>-<lang>.<ext>`，優先使用每張小於約 300 KB 的 WebP/JPEG。舊版 article object 以 `cover_image_zh`／`cover_image_en` 指向 `assets/covers/<file>`，未設定時使用自動生成 cover。

這些欄位與 `ARTICLES` array 屬於舊單檔 builder 設計，不保證在目前 template/renderer split 中存在。
