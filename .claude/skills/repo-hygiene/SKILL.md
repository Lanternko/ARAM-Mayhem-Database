---
name: repo-hygiene
description: "Route repository tidying, file placement, document archiving, worktree retirement, and local-file cleanup requests to the canonical repo hygiene runbook. Use for: 整理專案, 清 worktree, 檔案該放哪, 文件歸檔, 退役 worktree, 清理 outputs, repo cleanup, where should this file go."
metadata:
  version: "1.0"
  last_updated: "2026-11-10"
  status: active
  scope: project
---

# Repo hygiene adapter

1. Read `../../../runbooks/repo-hygiene.md` completely before moving, archiving, deleting, or retiring anything.
2. For worktree create/update/integrate/retire commands, follow `../../../runbooks/git-workflow.md`; the hygiene runbook only defines when to retire.
3. Run `python -m aram_nn.repo_audit` (read-only) to see the current FAIL/WARN list before acting; `pytest tests/test_repo_layout.py` must pass before committing structure or document changes.
4. Quarantine instead of deleting; never touch `data/`, `models/`, or `data/lcu/games.db*`, and never recurse into reparse points.

This adapter owns no layout rules, retention periods, or commands. If it conflicts with the runbook, the runbook wins and this file must be corrected.
