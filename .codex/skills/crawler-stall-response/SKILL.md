---
name: crawler-stall-response
description: Diagnose and recover from a live Mayhem crawler stall when workers are alive but data stops growing, queue items are low-yield, or LCU/auth/client state is unhealthy.
---

# Crawler Stall Adapter

1. Read `../../../runbooks/crawler-stall.md` completely before diagnosing or recovering the crawler.
2. Read `../../../OPERATIONS.md` for the current harness topology and production owner.
3. Preserve metrics/log evidence before mutation and use current CLI `--help` when needed.

This skill does not own worker counts, memory thresholds, seed-family conclusions, restart commands, or success criteria. The runbook and current production argv are authoritative.
