---
name: write-column
description: "Dormant guard for the unpublished ARAMMeta Articles/column feature. Use only when the user explicitly asks to revive, design, or write a site column/article; do not assume the old ARTICLES array still exists."
metadata:
  version: "2.0"
  last_updated: "2026-08-12"
  status: dormant
  scope: project
---

# Articles feature is dormant

The old skill edited an `ARTICLES` array inside a single-file `scripts/build_tier_list.py`. That architecture and the four-tab site description are superseded; Articles are not part of the current published information architecture.

If the user asks only to write prose, create a reviewable draft outside generated `docs/` and do not wire it into production. If the user explicitly asks to revive the product feature:

1. Read `../../../PRODUCT.md` and `../../../DESIGN.md`.
2. Inspect the current template/engine/renderer split and confirm the current page inventory.
3. Propose the content model, route, localization, cover asset, accessibility, performance, and publishing contract before implementation.
4. Use `../../../runbooks/site-deploy.md` only after the feature is implemented and approved for deployment.

Historical cover conventions are archived at `../../../notes/archive/article-cover-banners.md`; they are evidence, not a current contract.
