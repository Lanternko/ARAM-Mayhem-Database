"""Read-only repository layout and hygiene audit.

Rules live in ``runbooks/repo-hygiene.md``.  FAIL checks (T1-T7) run on the
``git ls-files`` listing and are mirrored by ``tests/test_repo_layout.py``;
WARN/INFO checks (L*, W*, S*) look at local state.  Nothing here writes to the
repository, the workspace or Git.

    python -m aram_nn.repo_audit [--repo PATH] [--workspace PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import unquote

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"

TOP_LEVEL_ALLOWLIST = frozenset(
    ".claude .codex .github .gitignore AGENTS.md CLAUDE.md CONTRIBUTING.md DESIGN.md "
    "MODEL.md OPERATIONS.md PRODUCT.md README.md docs documents pyproject.toml uv.lock "
    "runbooks scripts src tests".split()
)
ROOT_MD_ALLOWLIST = frozenset(
    "AGENTS.md CLAUDE.md CONTRIBUTING.md DESIGN.md MODEL.md OPERATIONS.md PRODUCT.md README.md".split()
)
MD_ALLOWED_DIRS = ("documents/", "runbooks/", ".claude/", ".codex/", ".github/")
SCRIPTS_SUBDIRS = frozenset("templates site_data launchers research ops site".split())
DOCUMENTS_SUBDIRS = frozenset("architecture guides reports archive".split())
REPORT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*\.md$")
BASELINE_RELPATH = "tests/fixtures/scripts_root_baseline.txt"

LOCAL_TOP_LEVEL_ALLOWLIST = frozenset(
    "data models logs outputs manual overwolf .venv dist build .git .claude .codex "
    ".impeccable .pytest_cache .ruff_cache".split()
)
# Never walked when measuring sizes: lineage/live data and dependency trees.
SIZE_SKIP_NAMES = frozenset({"data", "models", ".venv", ".git", "node_modules"})
PROTECTED_SUBSTRINGS = ("arammeta-publish-",)
PROTECTED_NAMES = frozenset({"aram-winrate-nn-api-runtime"})
ACTIVE_DAYS, BACKUP_DAYS, TRASH_DAYS = 7, 60, 14
SIZE_WALK_BUDGET_S = 20.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class Finding:
    id: str
    level: str
    message: str


# ---------------------------------------------------------------- git helpers

def run_git(repo: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def is_git_checkout(repo: Path) -> bool:
    try:
        return run_git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def git_ls_files(repo: Path) -> list[str]:
    proc = run_git(repo, "ls-files", "-z")
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    return [p for p in proc.stdout.split("\0") if p]


def load_scripts_baseline(repo: Path) -> set[str]:
    text = (repo / BASELINE_RELPATH).read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


# ------------------------------------------------------------- pure FAIL rules
# Every rule takes tracked paths (forward slashes, repo-relative) and returns a
# list of human-readable problems; an empty list means the rule passes.

def check_top_level(paths: Iterable[str]) -> list[str]:
    tops = {p.split("/", 1)[0] for p in paths}
    return [
        f"{t!r} is not an allowed tracked top-level item (allowlist: runbooks/repo-hygiene.md 'Tracked top level'); "
        "move it under documents/, runbooks/, scripts/<subdir>/ or src/"
        for t in sorted(tops - TOP_LEVEL_ALLOWLIST)
    ]


def check_scripts(paths: Iterable[str], baseline: set[str]) -> list[str]:
    problems = []
    for p in sorted(set(paths)):
        parts = p.split("/")
        if parts[0] != "scripts":
            continue
        if len(parts) == 2 and parts[1] not in baseline:
            problems.append(
                f"{p}: scripts/ root is frozen. Move the new file into scripts/research|ops|site|launchers/ "
                f"(importing only aram_nn); if the root really needs it, add it to {BASELINE_RELPATH} "
                "and update runbooks/repo-hygiene.md"
            )
        elif len(parts) > 2 and parts[1] not in SCRIPTS_SUBDIRS:
            problems.append(
                f"{p}: scripts/{parts[1]}/ is not an allowed subdirectory ({', '.join(sorted(SCRIPTS_SUBDIRS))})"
            )
    return problems


def check_markdown_locations(paths: Iterable[str]) -> list[str]:
    problems = []
    for p in sorted(set(paths)):
        if not p.lower().endswith(".md") or p.startswith("docs/"):
            continue
        name = posixpath.basename(p)
        if "/" not in p:
            ok = p in ROOT_MD_ALLOWLIST
        else:
            ok = p.startswith(MD_ALLOWED_DIRS) or name == "README.md"
        if not ok:
            problems.append(
                f"{p}: handwritten Markdown belongs at the root allowlist, documents/, runbooks/, "
                ".claude/, .codex/ or .github/ (or be a README.md)"
            )
    return problems


def check_report_names(paths: Iterable[str]) -> list[str]:
    problems = []
    for p in sorted(set(paths)):
        parts = p.split("/")
        if len(parts) == 3 and parts[:2] == ["documents", "reports"] and parts[2].endswith(".md"):
            if not REPORT_NAME_RE.match(parts[2]):
                problems.append(f"{p}: report names must be YYYY-MM-DD-kebab-slug.md")
    return problems


def check_documents_structure(paths: Iterable[str]) -> list[str]:
    problems = []
    for p in sorted(set(paths)):
        parts = p.split("/")
        if parts[0] != "documents":
            continue
        if len(parts) == 2 and parts[1] != "README.md":
            problems.append(f"{p}: documents/ root may only hold README.md")
        elif len(parts) > 2 and parts[1] not in DOCUMENTS_SUBDIRS:
            problems.append(f"{p}: documents/{parts[1]}/ is not one of {', '.join(sorted(DOCUMENTS_SUBDIRS))}")
    return problems


def _mentions(index_text: str, rel: str) -> bool:
    return re.search(rf"(?<![\w./-]){re.escape(rel)}(?![\w-])", index_text) is not None


def check_indexed(paths: Iterable[str], documents_index: str, runbooks_index: str) -> list[str]:
    problems = []
    for p in sorted(set(paths)):
        if p.startswith("documents/") and p.endswith(".md") and p != "documents/README.md":
            if not _mentions(documents_index, p[len("documents/"):]):
                problems.append(f"{p}: not listed in documents/README.md")
        elif p.startswith("runbooks/") and p.endswith(".md") and p.count("/") == 1 and p != "runbooks/README.md":
            if not _mentions(runbooks_index, p[len("runbooks/"):]):
                problems.append(f"{p}: not listed in runbooks/README.md")
    return problems


_FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_LINK_RE = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def extract_relative_links(markdown: str) -> list[str]:
    """Relative ``[text](target)`` targets, ignoring code blocks/spans, URLs and anchors."""
    text = _CODE_SPAN_RE.sub("", _FENCE_RE.sub("", markdown))
    out = []
    for m in _LINK_RE.finditer(text):
        target = m.group(1)
        if re.match(r"(?i)^(https?:|mailto:|tel:|#|[a-z][a-z0-9+.-]*://)", target):
            continue
        target = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if target:
            out.append(target)
    return out


def check_links(paths: Iterable[str], read_text: Callable[[str], str]) -> list[str]:
    tracked = set(paths)
    dirs = {"."}
    for p in tracked:
        d = posixpath.dirname(p)
        while d and d not in dirs:
            dirs.add(d)
            d = posixpath.dirname(d)
    problems = []
    for p in sorted(tracked):
        if not p.lower().endswith(".md") or p.startswith("docs/"):
            continue
        for target in extract_relative_links(read_text(p)):
            if target.startswith("/"):
                resolved = posixpath.normpath(target.lstrip("/"))
            else:
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(p), target))
            if resolved.startswith("..") or not (resolved in tracked or resolved in dirs):
                problems.append(f"{p}: broken relative link -> {target}")
    return problems


def run_fail_checks(repo: Path, paths: list[str]) -> list[Finding]:
    def read(rel: str) -> str:
        try:
            return (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    results = {
        "T1": check_top_level(paths),
        "T2": check_scripts(paths, load_scripts_baseline(repo)),
        "T3": check_markdown_locations(paths),
        "T4": check_report_names(paths),
        "T5": check_documents_structure(paths),
        "T6": check_indexed(paths, read("documents/README.md"), read("runbooks/README.md")),
        "T7": check_links(paths, read),
    }
    return [Finding(rule, FAIL, msg) for rule, msgs in results.items() for msg in msgs]


# ------------------------------------------------------------- filesystem walk

def is_reparse_entry(entry: os.DirEntry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE) or entry.is_symlink()


def _is_inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:  # different drives
        return False


def find_reparse_points(root: str | os.PathLike[str]) -> list[tuple[str, str, bool]]:
    """Reparse points below ``root`` as ``(path, target, target_is_inside_root)``.

    Never descends into a reparse point (junctions/symlinks), so it cannot be
    led out of ``root``.
    """
    root_s = os.path.realpath(os.fspath(root))
    found: list[tuple[str, str, bool]] = []
    stack = [os.fspath(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if is_reparse_entry(entry):
                target = os.path.realpath(entry.path)
                found.append((entry.path, target, _is_inside(target, root_s)))
            elif entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
    return found


def dir_size(path: Path, budget_s: float = SIZE_WALK_BUDGET_S) -> tuple[int, bool]:
    """Total file bytes under ``path`` without entering reparse points or skip-listed dirs.

    Returns ``(bytes, complete)``; ``complete`` is False when the time budget ran out.
    """
    deadline = time.monotonic() + budget_s
    total = 0
    stack = [os.fspath(path)]
    while stack:
        if time.monotonic() > deadline:
            return total, False
        try:
            with os.scandir(stack.pop()) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if is_reparse_entry(entry):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SIZE_SKIP_NAMES:
                        stack.append(entry.path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total, True


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


# ------------------------------------------------------------ local WARN/INFO

def default_workspace(repo: Path) -> Path:
    """``<repo parent>/aram-winrate-nn-workspace``; a task worktree resolves to its own workspace."""
    repo = repo.resolve()
    if repo.parent.name == "worktrees" and repo.parent.parent.name == "aram-winrate-nn-workspace":
        return repo.parent.parent
    return repo.parent / "aram-winrate-nn-workspace"


def check_local_top_level(repo: Path, tracked_tops: set[str]) -> list[Finding]:
    # An allowlisted tracked name that is not tracked yet (e.g. uv.lock) is not an orphan.
    known = tracked_tops | LOCAL_TOP_LEVEL_ALLOWLIST | TOP_LEVEL_ALLOWLIST
    found = []
    for entry in sorted(os.scandir(repo), key=lambda e: e.name):
        if entry.name not in known:
            found.append(Finding("L1", WARN, f"untracked top-level orphan: {entry.name}"))
    return found


def check_outputs_root(repo: Path) -> list[Finding]:
    outputs = repo / "outputs"
    if not outputs.is_dir():
        return []
    with os.scandir(outputs) as it:
        n = sum(1 for e in it if e.is_file(follow_symlinks=False))
    level = WARN if n else INFO
    return [Finding("L2", level, f"outputs/ root holds {n} loose file(s); outputs/ root should hold none")]


def parse_worktrees(porcelain: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in porcelain.strip().split("\n\n"):
        wt: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            wt[key] = value
        if "worktree" in wt:
            out.append(wt)
    return out


def _latest_activity(repo: Path, wt_path: str, head: str) -> float:
    stamps = []
    proc = run_git(Path(wt_path), "log", "-1", "--format=%ct", head or "HEAD")
    if proc.returncode == 0 and proc.stdout.strip().isdigit():
        stamps.append(float(proc.stdout.strip()))
    gitdir = run_git(Path(wt_path), "rev-parse", "--absolute-git-dir").stdout.strip()
    for name in ("index", "HEAD"):
        try:
            stamps.append(os.stat(os.path.join(gitdir, name)).st_mtime)
        except OSError:
            pass
    status = run_git(Path(wt_path), "status", "--short", "-z").stdout
    for item in status.split("\0"):
        rel = item[3:] if len(item) > 3 else ""
        if rel:
            try:
                stamps.append(os.stat(os.path.join(wt_path, rel)).st_mtime)
            except OSError:
                pass
    return max(stamps, default=0.0)


def classify_worktree(repo: Path, wt: dict[str, str], primary: str, now: float) -> tuple[str, str]:
    """Return ``(category, evidence)`` following the order in runbooks/repo-hygiene.md."""
    path = wt["worktree"]
    norm = os.path.normcase(os.path.normpath(path))
    name = os.path.basename(norm)
    if norm == os.path.normcase(os.path.normpath(primary)) or name in PROTECTED_NAMES or any(
        s in norm for s in PROTECTED_SUBSTRINGS
    ):
        return "protected", "primary / api-runtime / publisher temp"
    if norm == os.path.normcase(os.path.normpath(str(repo))):
        return "protected", "the checkout being audited"
    if not os.path.isdir(path):
        return "keep", "directory missing (prunable)"
    wt_path = Path(path)
    head = wt.get("HEAD", "")
    activity = _latest_activity(repo, path, head)
    if now - activity < ACTIVE_DAYS * 86400:
        return "active", "activity within 7 days"
    status = run_git(wt_path, "status", "--short").stdout.splitlines()
    dirty = [line for line in status if not line.startswith("??")]
    if dirty:
        return "keep", f"{len(dirty)} tracked change(s)"
    untracked = len(status)
    if run_git(repo, "merge-base", "--is-ancestor", head, "origin/main").returncode == 0:
        external = [r for r in find_reparse_points(path) if not r[2]]
        if external:
            return "keep", f"reparse point(s) leaving the worktree: {external[0][0]} -> {external[0][1]}"
        if untracked:
            return "merged-with-untracked", f"ancestor of origin/main; {untracked} untracked file(s)"
        return "retire-candidate", "clean, ancestor of origin/main"
    merged = run_git(repo, "merge-tree", "--write-tree", "origin/main", head)
    main_tree = run_git(repo, "rev-parse", "origin/main^{tree}").stdout.strip()
    if merged.returncode == 0 and merged.stdout.split("\n", 1)[0].strip() == main_tree:
        return "content-merged", "merge into origin/main changes nothing (needs bundle before retiring; keep branch)"
    ahead = run_git(repo, "rev-list", "--count", f"origin/main..{head}").stdout.strip()
    return "keep", f"unmerged ({ahead} ahead of origin/main)"


def check_worktrees(repo: Path, workspace: Path) -> list[Finding]:
    proc = run_git(repo, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return [Finding("W1", INFO, "git worktree list failed; skipped")]
    worktrees = parse_worktrees(proc.stdout)
    if not worktrees:
        return []
    if run_git(repo, "rev-parse", "--verify", "-q", "origin/main").returncode != 0:
        return [Finding("W1", INFO, "origin/main not found; worktree classification skipped")]
    primary = worktrees[0]["worktree"]
    managed = os.path.normcase(os.path.normpath(str(workspace / "worktrees")))
    now = time.time()
    findings = []
    for wt in worktrees:
        category, evidence = classify_worktree(repo, wt, primary, now)
        label = f"{wt['worktree']} [{category}] {evidence}"
        retire = category in {"retire-candidate", "merged-with-untracked", "content-merged"}
        findings.append(Finding("W1", WARN if retire else INFO, label))
        norm = os.path.normcase(os.path.normpath(wt["worktree"]))
        if category not in {"protected", "active"} and not norm.startswith(managed + os.sep) and ".claude" + os.sep + "worktrees" not in norm:
            findings.append(Finding("W1", WARN, f"{wt['worktree']}: outside {workspace / 'worktrees'} (git worktree move when idle)"))
    findings.append(Finding("W1", INFO, "in-use (process) check is not performed; verify before retiring"))
    return findings


def check_merged_branches(repo: Path) -> list[Finding]:
    if run_git(repo, "rev-parse", "--verify", "-q", "origin/main").returncode != 0:
        return []
    merged = run_git(repo, "branch", "--merged", "origin/main", "--format=%(refname:short)").stdout.split()
    checked_out = {
        wt["branch"].removeprefix("refs/heads/")
        for wt in parse_worktrees(run_git(repo, "worktree", "list", "--porcelain").stdout)
        if "branch" in wt
    }
    return [
        Finding("W2", WARN, f"local branch {b} is merged into origin/main and has no worktree (git branch -d)")
        for b in sorted(merged)
        if b != "main" and b not in checked_out
    ]


def check_workspace_age(workspace: Path, now: float | None = None) -> list[Finding]:
    now = time.time() if now is None else now
    findings = []
    for sub, days in (("backups", BACKUP_DAYS), ("trash", TRASH_DAYS)):
        base = workspace / sub
        if not base.is_dir():
            continue
        for entry in sorted(os.scandir(base), key=lambda e: e.name):
            try:
                age = (now - entry.stat(follow_symlinks=False).st_mtime) / 86400
            except OSError:
                continue
            if age > days:
                findings.append(Finding("W3", WARN, f"{sub}/{entry.name} is {age:.0f} days old (> {days}); owner decides on deletion"))
    return findings


def check_sizes(repo: Path, workspace: Path) -> list[Finding]:
    findings = []
    targets = [repo / n for n in ("logs", "outputs", ".codex", ".claude")]
    if workspace.is_dir():
        targets += [Path(e.path) for e in sorted(os.scandir(workspace), key=lambda e: e.name) if e.is_dir(follow_symlinks=False)]
    for t in targets:
        if t.is_dir():
            size, complete = dir_size(t)
            findings.append(Finding("S1", INFO, f"{t}: {_fmt_size(size)}{'' if complete else '+ (time budget reached)'}"))
    anchor = workspace if workspace.exists() else repo
    usage = shutil.disk_usage(anchor)
    findings.append(Finding("S1", INFO, f"free space on {os.path.splitdrive(str(anchor.resolve()))[0] or anchor}: {_fmt_size(usage.free)}"))
    return findings


def run_audit(repo: Path, workspace: Path) -> list[Finding]:
    paths = git_ls_files(repo)
    findings = run_fail_checks(repo, paths)
    tops = {p.split("/", 1)[0] for p in paths}
    findings += check_local_top_level(repo, tops)
    findings += check_outputs_root(repo)
    findings += check_worktrees(repo, workspace)
    findings += check_merged_branches(repo)
    findings += check_workspace_age(workspace)
    findings += check_sizes(repo, workspace)
    return findings


def summarize(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {FAIL: 0, WARN: 0, INFO: 0}
    for f in findings:
        counts[f.level] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only repository layout audit (see runbooks/repo-hygiene.md).")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    repo = args.repo.resolve()
    workspace = (args.workspace or default_workspace(repo)).resolve()
    if not is_git_checkout(repo):
        print(f"{repo} is not a Git checkout", file=sys.stderr)
        return 2
    findings = run_audit(repo, workspace)
    counts = summarize(findings)
    if args.json:
        print(json.dumps({"repo": str(repo), "workspace": str(workspace), "counts": counts,
                          "findings": [asdict(f) for f in findings]}, ensure_ascii=False, indent=2))
    else:
        for level in (FAIL, WARN, INFO):
            for f in findings:
                if f.level == level:
                    print(f"[{f.level}] {f.id}: {f.message}")
        print(f"FAIL={counts[FAIL]} WARN={counts[WARN]} INFO={counts[INFO]}")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
