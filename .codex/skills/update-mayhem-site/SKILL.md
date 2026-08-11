---
name: update-mayhem-site
description: Refresh, deploy, upload, publish, or 上線 the ARAM Mayhem GitHub Pages tier-list website for this repo. Use for site publishing actions and requests to inspect or change the deploy SOP.
---

# Update Mayhem Site Adapter

1. Read `../../../runbooks/site-deploy.md` completely before taking any action.
2. Select exactly one site lane from the requested scope: routine data, frontend shell, or generator/schema. If the change is only deploy documentation, skill adapters, tests, or internal publish tooling and does not change live artifacts, use the runbook's repository-only integration rule instead of inventing a site rebuild.
3. Treat an explicit deploy/publish/ship/live-site request as authorization to complete the matching build, verification, commit, and push; do not ask for the same confirmation again. A change/preview request without deploy intent stays local.
4. Follow the runbook's isolated publisher, canonical allowlist, failure routing, and verification.
5. For UI changes, also read `../../../DESIGN.md`.

This skill is an auto-discovery adapter. It owns no duplicate commands or artifact list; `runbooks/site-deploy.md` is authoritative.
