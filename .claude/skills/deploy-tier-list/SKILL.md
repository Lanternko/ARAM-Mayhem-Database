---
name: deploy-tier-list
description: "Route ARAMMeta tier-list data refresh, new-game statistics, patch refresh, or GitHub Pages data publishing requests to the canonical site deploy runbook. Use for: deploy tier list data, publish tier list, refresh win rates, update patch data, 更新勝率, 更新網站資料."
metadata:
  version: "2.0"
  last_updated: "2026-08-12"
  status: active
  scope: project
---

# Tier-list deploy adapter

1. Read `../../../runbooks/site-deploy.md` completely before taking any action.
2. Select the **routine data publish** lane for new games, win rates, patch data, tiers, augments, radar, or axes. Frontend-only scope belongs to the `deploy-shell` adapter; schema/statistics logic belongs to the runbook's generator/schema lane.
3. An explicit deploy/publish/ship/live-site request already authorizes the matching build, verification, commit, and push; do not insert a preliminary check followed by another confirmation.
4. Follow the runbook's isolated publisher, `DEFAULT_DOC_PATHS`, failure routing, and live verification rules.
5. Use current CLI `--help` when an option is uncertain.

This adapter intentionally contains no command copy, generated artifact allowlist, timing estimate, commit recipe, or manual staging list. The runbook is authoritative.
