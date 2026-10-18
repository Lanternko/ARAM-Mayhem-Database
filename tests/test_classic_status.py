from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from aram_nn.site.classic_status import write_classic_status_pages


def test_classic_status_replaces_all_locales_without_publishing_stale_rankings(tmp_path):
    pages = write_classic_status_pages(tmp_path)
    assert len(pages) == 3
    for relative, lang, title, prefix in (
        ("classic.html", "zh-Hant", "經典模式已停用", ""),
        ("zh-cn/classic.html", "zh-Hans", "经典模式已停用", "/zh-cn"),
        ("en/classic.html", "en", "Classic is unavailable", "/en"),
    ):
        page = (tmp_path / relative).read_text(encoding="utf-8")
        assert f"<html lang='{lang}'>" in page
        assert f"<h1>{title}</h1>" in page
        assert f"href='https://arammeta.com/{relative}'" in page
        assert f"href='{prefix}/'" in page
        assert f"href='{prefix}/feedback/'" in page
        assert "noindex,follow" in page
        assert "2026-10-18" in page
        assert "queue 4310" in page
        assert "classic-data" not in page
        assert "mode-menu" not in page
        assert "site.js" not in page
        assert "fetch(" not in page
        assert page.count("rel='alternate'") == 3


def test_legacy_classic_publisher_command_writes_status_without_db_or_metadata(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("classic_status_cli", Path(__file__).parents[1] / "scripts/build_classic_page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(module, "collect_stats", side_effect=AssertionError("must not scan DB")), patch.object(
        module, "load_classic_champion_metadata", side_effect=AssertionError("must not fetch metadata")
    ):
        result = CliRunner().invoke(module.main, ["--db", str(tmp_path / "missing.db"), "--recent-patches", "2", "--out", str(tmp_path / "classic.html")])
    assert result.exit_code == 0, result.output
    assert "經典模式已停用" in (tmp_path / "classic.html").read_text(encoding="utf-8")
    assert not (tmp_path / "missing.db").exists()
