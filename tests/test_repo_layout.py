"""Repository layout contract (runbooks/repo-hygiene.md).

The real-repo tests run the T1-T7 rules from ``aram_nn.repo_audit`` against
``git ls-files``; the synthetic tests pin each rule's pass/fail behaviour.
Nothing here opens a window or writes outside ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))  # never import another checkout's aram_nn

from aram_nn import repo_audit as ra  # noqa: E402


def _git_available() -> bool:
    try:
        return ra.is_git_checkout(REPO)
    except (OSError, subprocess.SubprocessError):
        return False


needs_git = pytest.mark.skipif(not _git_available(), reason="not a Git checkout or git unavailable")

BASELINE = {"a.py", "b.py"}
INDEX_DOCS = "see architecture/x.md and reports/2026-01-02-slug.md"


# --------------------------------------------------------------- real repo

@pytest.fixture(scope="module")
def tracked() -> list[str]:
    return ra.git_ls_files(REPO)


@needs_git
def test_t1_top_level_allowlist(tracked):
    assert ra.check_top_level(tracked) == []


@needs_git
def test_top_level_is_exactly_the_contract(tracked):
    tops = {p.split("/", 1)[0] for p in tracked}
    for gone in ("notes", "PLAN.md", "augment-category-editor.html", "start_recommender.bat"):
        assert gone not in tops


@needs_git
def test_t2_scripts_root_is_frozen(tracked):
    baseline = ra.load_scripts_baseline(REPO)
    assert len(baseline) >= 123 and "build_champion_icons.py" in baseline
    assert ra.check_scripts(tracked, baseline) == []


@needs_git
def test_t3_markdown_locations(tracked):
    assert ra.check_markdown_locations(tracked) == []


@needs_git
def test_t4_report_names(tracked):
    assert ra.check_report_names(tracked) == []


@needs_git
def test_t5_documents_structure(tracked):
    assert ra.check_documents_structure(tracked) == []


@needs_git
def test_t6_every_document_is_indexed(tracked):
    docs = (REPO / "documents" / "README.md").read_text(encoding="utf-8")
    runbooks = (REPO / "runbooks" / "README.md").read_text(encoding="utf-8")
    assert ra.check_indexed(tracked, docs, runbooks) == []


@needs_git
def test_t7_relative_links_resolve(tracked):
    def read(rel):
        return (REPO / rel).read_text(encoding="utf-8", errors="replace")

    assert ra.check_links(tracked, read) == []


# --------------------------------------------------------- synthetic rules

def test_t1_flags_unknown_top_level():
    assert ra.check_top_level(["README.md", "src/a.py", "scripts/x.py"]) == []
    assert any("notes" in m for m in ra.check_top_level(["notes/a.md", "README.md"]))
    assert any("start_recommender.bat" in m for m in ra.check_top_level(["start_recommender.bat"]))


def test_t2_scripts_root_and_subdirs():
    assert ra.check_scripts(["scripts/a.py", "scripts/ops/new.py", "scripts/templates/t.html"], BASELINE) == []
    new_root = ra.check_scripts(["scripts/new_tool.py"], BASELINE)
    assert new_root and "baseline" in new_root[0] and "scripts/research" in new_root[0]
    assert ra.check_scripts(["scripts/misc/x.py"], BASELINE)


def test_t3_markdown_placement():
    ok = ["README.md", "documents/a.md", "runbooks/b.md", ".claude/skills/x/SKILL.md", "scripts/README.md", "docs/x.md"]
    assert ra.check_markdown_locations(ok) == []
    assert ra.check_markdown_locations(["NOTES.md"])
    assert ra.check_markdown_locations(["tests/notes.md"])
    assert ra.check_markdown_locations(["notes/idea.md"])


def test_t4_report_name_pattern():
    assert ra.check_report_names(["documents/reports/2026-11-10-repo-reorganization.md", "documents/archive/Any_Name.md"]) == []
    for bad in ("final_report_2026_06_14.md", "2026-6-1-x.md", "2026-06-14-Upper.md", "README.md"):
        assert ra.check_report_names([f"documents/reports/{bad}"]), bad


def test_t5_documents_children():
    ok = ["documents/README.md", "documents/guides/g.md", "documents/archive/design-experiments/z.md"]
    assert ra.check_documents_structure(ok) == []
    assert ra.check_documents_structure(["documents/stray.md"])
    assert ra.check_documents_structure(["documents/notes/x.md"])


def test_t6_index_must_list_documents_and_runbooks():
    paths = ["documents/architecture/x.md", "documents/reports/2026-01-02-slug.md", "documents/README.md", "runbooks/a.md"]
    assert ra.check_indexed(paths, INDEX_DOCS, "[a](a.md)") == []
    missing = ra.check_indexed(paths + ["documents/guides/new.md"], INDEX_DOCS, "[a](a.md)")
    assert missing == ["documents/guides/new.md: not listed in documents/README.md"]
    assert ra.check_indexed(["runbooks/b.md"], INDEX_DOCS, "[a](a.md)")
    # a path that only appears as the tail of a different path does not count
    assert ra.check_indexed(["documents/archive/x.md"], "see architecture/x.md", "")


def test_t7_link_extraction_ignores_code_urls_and_anchors():
    md = "[a](b.md) [u](https://x.y/z) [m](mailto:a@b.c) [h](#top) `[c](code.md)`\n```\n[f](fence.md)\n```\n[t](c.md#sec \"title\")"
    assert ra.extract_relative_links(md) == ["b.md", "c.md"]


def test_t7_link_resolution():
    files = {"documents/README.md": "[ok](archive/a.md) [dir](archive) [up](../README.md)", "documents/archive/a.md": "", "README.md": ""}
    assert ra.check_links(list(files), files.__getitem__) == []
    files["documents/README.md"] = "[bad](archive/missing.md) [esc](../../outside.md)"
    problems = ra.check_links(list(files), files.__getitem__)
    assert len(problems) == 2 and all("broken relative link" in p for p in problems)
    # generated docs/ is not checked
    assert ra.check_links(["docs/x.md"], lambda p: "[x](nope.md)") == []


def test_parse_worktrees_porcelain():
    text = "worktree C:/a\nHEAD 111\nbranch refs/heads/main\n\nworktree C:/b\nHEAD 222\ndetached\n"
    wts = ra.parse_worktrees(text)
    assert [w["worktree"] for w in wts] == ["C:/a", "C:/b"] and wts[0]["branch"] == "refs/heads/main"


def test_l1_flags_only_unknown_local_top_level(tmp_path):
    for name in (".ruff_cache", "outputs", "src", "stray"):
        (tmp_path / name).mkdir()
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    msgs = [f.message for f in ra.check_local_top_level(tmp_path, {"src"})]
    assert msgs == ["untracked top-level orphan: stray"]


def test_workspace_age_thresholds(tmp_path):
    (tmp_path / "backups" / "old").mkdir(parents=True)
    (tmp_path / "backups" / "new").mkdir()
    (tmp_path / "trash" / "stale").mkdir(parents=True)
    now = 1_000_000_000.0
    os.utime(tmp_path / "backups" / "old", (now - 61 * 86400, now - 61 * 86400))
    os.utime(tmp_path / "backups" / "new", (now - 59 * 86400, now - 59 * 86400))
    os.utime(tmp_path / "trash" / "stale", (now - 15 * 86400, now - 15 * 86400))
    msgs = [f.message for f in ra.check_workspace_age(tmp_path, now=now)]
    assert len(msgs) == 2 and any("backups/old" in m for m in msgs) and any("trash/stale" in m for m in msgs)


# ----------------------------------------------------------- reparse points

def test_find_reparse_points_does_not_follow_junction(tmp_path):
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "secret.txt").write_text("x", encoding="utf-8")
    link = root / "sub" / "link"
    try:
        _winapi.CreateJunction(str(outside), str(link))
    except OSError as exc:  # pragma: no cover - depends on filesystem
        pytest.skip(f"cannot create junction: {exc}")
    inner = root / "inner"
    inner.mkdir()

    found = ra.find_reparse_points(root)
    assert [(Path(p), inside) for p, _, inside in found] == [(link, False)]
    assert Path(found[0][1]).resolve() == outside.resolve()
    # sizes never enter the junction either
    (root / "sub" / "own.txt").write_text("abc", encoding="utf-8")
    assert ra.dir_size(root)[0] == 3
    assert (outside / "deep" / "secret.txt").exists()


def test_find_reparse_points_empty_tree(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert ra.find_reparse_points(tmp_path) == []
