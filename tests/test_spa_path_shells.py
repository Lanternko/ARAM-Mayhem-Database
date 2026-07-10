from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tierlist_render import (  # noqa: E402
    _site_base_href,
    _spa_deep_link_stub,
    discover_column_article_ids,
    write_spa_path_shells,
)


class SpaPathShellTests(unittest.TestCase):
    def test_site_base_href(self) -> None:
        self.assertEqual(_site_base_href("https://arammeta.com/"), "https://arammeta.com/")
        self.assertEqual(_site_base_href("https://arammeta.com"), "https://arammeta.com/")
        self.assertEqual(_site_base_href(""), "")

    def test_discover_column_article_ids(self) -> None:
        ids = discover_column_article_ids()
        self.assertIn("sprees-not-snowball", ids)
        self.assertIn("how-to-read", ids)
        self.assertTrue(all(isinstance(x, str) and x for x in ids))

    def test_deep_link_stub_stashes_path(self) -> None:
        html = _spa_deep_link_stub(
            site_url="https://arammeta.com/",
            og_image="https://arammeta.com/og-image.png",
            canonical_path="/column/how-to-read",
            title="How to read · arammeta",
            description="guide",
        )
        self.assertIn("sessionStorage.setItem('aram-spa-path'", html)
        self.assertIn("location.replace('/')", html)
        self.assertIn("https://arammeta.com/column/how-to-read", html)
        self.assertIn("og:image", html)

    def test_write_spa_path_shells_creates_article_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.html"
            index.write_text("<!doctype html><title>app</title>", encoding="utf-8")
            written = write_spa_path_shells(
                index,
                site_url="https://arammeta.com/",
                og_image="https://arammeta.com/og-image.png",
            )
            self.assertTrue(any(p.name == "404.html" for p in written))
            article = root / "column" / "sprees-not-snowball" / "index.html"
            self.assertTrue(article.is_file())
            body = article.read_text(encoding="utf-8")
            self.assertLess(article.stat().st_size, 4_000)  # stub, not full SPA
            self.assertIn("/column/sprees-not-snowball", body)
            self.assertIn("aram-spa-path", body)


if __name__ == "__main__":
    unittest.main()
