---
name: write-column
description: "Write or edit an ARAMMeta column article (專欄). Use when the user asks to write, revise, or add a site article; articles are static pages built from scripts/templates/column/, not an app view."
metadata:
  version: "3.0"
  last_updated: "2026-11-19"
  status: active
  scope: project
---

# Column articles

Articles are static, crawlable pages at `/column/<id>/` with `/en` and `/zh-cn` mirrors. The page builder is `src/aram_nn/site/column.py`; it owns the content contract (read its module docstring first). Do not edit generated `docs/` and do not revive the old `ARTICLES` array in `site.js`.

To add or change an article:

1. Read `../../../PRODUCT.md` and `../../../DESIGN.md` for tone and evidence rules.
2. Edit `scripts/templates/column/articles.json` (metadata, newest first) and the `<id>.zh.html` / `<id>.en.html` body fragments. zh-Hans is derived from zh-Hant at build time; never hand-write it.
3. State the metric, the sample, the patch range and the limits in the article itself. Use the real publication date; do not re-date old analysis.
4. Run `tests/test_column_pages.py` (it enforces both languages and a minimum length).
5. Deploy only through `../../../runbooks/site-deploy.md` (frontend shell lane) after the user approves.

Historical cover conventions are archived at `../../../documents/archive/article-cover-banners.md`; they are evidence, not a current contract.
