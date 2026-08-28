---
name: opgg-seed-refresh
description: Refresh TW League OPGG seed pages and hydrate the local Mayhem crawler frontier when the current seed window is exhausted.
---

# OPGG Seed Refresh Adapter

1. Read `../../../runbooks/opgg-seed-refresh.md` completely before taking action.
2. If the symptom has not yet been classified as seed exhaustion, read and follow `../../../runbooks/crawler-stall.md` first.
3. Let the production watchdog own the long-running worker fleet.

This skill owns no separate page range, worker count, refresh command, or recovery conclusion. The runbook and current CLI `--help` are authoritative.
