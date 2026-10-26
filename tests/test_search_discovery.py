from pathlib import Path
from xml.etree import ElementTree as ET

from aram_nn.site.search_discovery import SITEMAP_NS, XHTML_NS, write_search_discovery


def page(root, route, *, canonical=None, extra=""):
    path = root / route / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = "https://arammeta.com/" + (route + "/" if route else "")
    path.write_text(
        f"<html><head><link rel='canonical' href='{canonical or url}'>{extra}</head><body>content</body></html>",
        encoding="utf-8",
    )
    return path


def test_discovery_excludes_redirects_noindex_aliases_and_stale_pages(tmp_path):
    home = page(tmp_path, "")
    pool = page(tmp_path, "augments/pools")
    hidden = page(tmp_path, "p/player-history", extra="<meta name='robots' content='noindex, follow'>")
    redirect = page(tmp_path, "champions/ahri", extra="<meta http-equiv='refresh' content='0;url=/'>")
    alias = page(tmp_path, "contact", canonical="https://arammeta.com/feedback/")
    foreign = page(tmp_path, "foreign", canonical="https://other.example/foreign/")
    page(tmp_path, "unpublished-column")  # Not part of this build's explicit output list.
    outputs = write_search_discovery(
        tmp_path, [home, pool, hidden, redirect, alias, foreign], site_url="https://arammeta.com/",
    )
    tree = ET.parse(outputs[0])
    assert [n.text for n in tree.findall(f".//{{{SITEMAP_NS}}}loc")] == [
        "https://arammeta.com/", "https://arammeta.com/augments/pools/",
    ]
    assert "lastmod" not in outputs[0].read_text()
    assert "Sitemap: https://arammeta.com/sitemap.xml" in outputs[1].read_text()
    assert "Disallow" not in outputs[1].read_text()


def test_locale_alternates_require_existing_reciprocal_canonical_pages(tmp_path):
    links = (
        "<link rel='alternate' hreflang='zh-Hant' href='https://arammeta.com/'>"
        "<link rel='alternate' hreflang='en' href='https://arammeta.com/en/'>"
        "<link rel='alternate' hreflang='zh-Hans' href='https://arammeta.com/zh-cn/'>"
        "<link rel='alternate' hreflang='x-default' href='https://arammeta.com/'>"
    )
    home = page(tmp_path, "", extra=links)
    en = page(tmp_path, "en", extra=links)
    # A missing Simplified Chinese page must not be claimed as an alternate.
    outputs = write_search_discovery(tmp_path, [home, en], site_url="https://arammeta.com/")
    tree = ET.parse(outputs[0])
    for node in tree.findall(f"{{{SITEMAP_NS}}}url"):
        assert {n.get("hreflang") for n in node.findall(f"{{{XHTML_NS}}}link")} == {
            "zh-Hant", "en", "x-default",
        }


def test_local_preview_does_not_emit_public_crawler_settings(tmp_path):
    home = page(tmp_path, "")
    assert write_search_discovery(tmp_path, [home], site_url="") == []
    assert not (tmp_path / "robots.txt").exists()


def _build_page(tmp_path, route, build_script):
    path = tmp_path / route / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = "https://arammeta.com/" + (route + "/" if route else "")
    path.write_text(
        f"<html><head><link rel='canonical' href='{url}'>{build_script}</head><body>x</body></html>",
        encoding="utf-8",
    )
    return path


def test_sitemap_lastmod_is_the_data_snapshot_build_date(tmp_path):
    dated = _build_page(
        tmp_path, "champions/aatrox",
        "<script>window.__ARAM_BUILD__={\"patchLabel\":\"patch 26.21\","
        "\"buildDate\":\"2026-10-26\"}</script>",
    )
    about = page(tmp_path, "about")
    outputs = write_search_discovery(
        tmp_path, [dated, about], site_url="https://arammeta.com/",
    )
    tree = ET.parse(outputs[0])
    lastmods = {
        node.findtext(f"{{{SITEMAP_NS}}}loc"): node.findtext(f"{{{SITEMAP_NS}}}lastmod")
        for node in tree.findall(f"{{{SITEMAP_NS}}}url")
    }
    assert lastmods == {
        "https://arammeta.com/champions/aatrox/": "2026-10-26",
        "https://arammeta.com/about/": None,
    }


def test_sitemap_skips_malformed_or_missing_build_date(tmp_path):
    malformed = _build_page(
        tmp_path, "champions/ahri",
        "<script>window.__ARAM_BUILD__={\"buildDate\":\"2026-1-2\"}</script>",
    )
    broken_json = _build_page(
        tmp_path, "champions/jinx",
        "<script>window.__ARAM_BUILD__={buildDate: nope}</script>",
    )
    no_date = _build_page(
        tmp_path, "champions/kaisa",
        "<script>window.__ARAM_BUILD__={\"patchLabel\":\"patch 26.21\"}</script>",
    )
    outputs = write_search_discovery(
        tmp_path, [malformed, broken_json, no_date], site_url="https://arammeta.com/",
    )
    assert "lastmod" not in outputs[0].read_text(encoding="utf-8")
