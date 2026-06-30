# Article cover banners (專欄 首圖)

Hand-made 16:9 cover images for column articles. When an article sets
`cover_image_zh` / `cover_image_en`, the card + reader hero use the image
instead of the auto-generated vector cover (`articleCover()` in
`scripts/build_tier_list.py`). Articles without these fields keep the SVG cover.

## Convention

- **Aspect ratio:** 16:9 (e.g. 1920×1080 or 1600×900). The whole image shows in
  the reader hero (uncropped) and is `object-fit: cover` in the small card.
- **Filename:** `<article-id>-<lang>.<ext>` — e.g. `draw-your-sword-zh.webp`,
  `draw-your-sword-en.webp`. The article id is the `id:` in the `ARTICLES` array.
- **Format:** prefer `.webp` or `.jpg` (these are photographic banners; keep each
  under ~300 KB so the page stays light). `.png` works too.
- **Referenced as:** `cover_image_zh: 'assets/covers/<file>'` in the article object.

The build mirrors this directory into the preview output (`outputs/assets/covers/`)
so previews resolve the same relative URLs; on the live site it ships from
`docs/assets/covers/`.
