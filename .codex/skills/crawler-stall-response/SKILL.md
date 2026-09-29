---
name: crawler-stall-response
description: Diagnose Mayhem crawler stall alerts, explain why automatic recovery did or did not run, and recover ongoing stalls involving watchdog availability, resource guards, LCU/auth, or low-yield seeds.
---

# Crawler Stall Adapter

1. Read [the stall runbook](../../../runbooks/crawler-stall.md) completely and [OPERATIONS.md](../../../OPERATIONS.md) for harness topology and production ownership. Locate the live checkout; an isolated task worktree does not contain current runtime evidence.
2. For an alert or “why no automatic recovery?” question, correlate the incident and subsequent recovery timeline before choosing an action. Distinguish watchdog absence, guarded pauses, failed recovery, recovery in progress, and already recovered collection.
3. Preserve relevant evidence before mutation. A diagnosis request does not by itself request a restart or tuning change; if recovery is requested and still needed, follow the runbook and current CLI `--help`.
4. Report the incident cause and current state separately, with timestamps, scope, evidence, and remaining uncertainty. Claim Mayhem/current-patch recovery only when scoped growth supports it.

This skill does not own worker counts, memory thresholds, seed-family conclusions, restart commands, or success criteria. The runbook and current production argv are authoritative.
