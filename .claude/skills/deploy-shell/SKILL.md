---
name: deploy-shell
description: "Route frontend-only, CSS-only, JS-only, copy, layout, or shell-only ARAMMeta deploy requests to the canonical site deploy runbook. Use for: deploy shell, deploy frontend, deploy css, 只改 CSS 部署, 只改前端, 前端部署."
metadata:
  version: "2.0"
  last_updated: "2026-08-12"
  status: active
  scope: project
---

# Frontend deploy adapter

1. Read `../../../runbooks/site-deploy.md` completely before taking any action.
2. Select the **frontend shell publish** lane. CSS, JS, copy, layout, HTML shell, and SEO-only changes must not be promoted to a full data build.
3. An explicit deploy/publish/ship/live-site request already authorizes the matching build, verification, commit, and push; do not ask again. Without deploy intent, stop after local preview and verification.
4. Read `../../../DESIGN.md` before changing or shipping UI.
5. Use current CLI `--help` when an option is uncertain.

This adapter owns no commands, timings, generated artifact list, branch workflow, or rollback policy. If it conflicts with the runbook, the runbook wins and this file must be corrected.
