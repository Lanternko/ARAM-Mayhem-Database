from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_wiki_hd_splashes as splashes  # noqa: E402


class DraftSplashSourceTests(unittest.TestCase):
    def build(self, previous: dict, lookup) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tier = root / "tier.json"
            out = root / "splashes.json"
            tier.write_text(json.dumps({"champs": {"32": {
                "alias": "Amumu", "name_en": "Amumu",
            }}}), encoding="utf-8")
            out.write_text(json.dumps({"byCid": previous}), encoding="utf-8")
            with (
                patch.object(splashes, "TIER", tier),
                patch.object(splashes, "OUT_JSON", out),
                patch.object(splashes, "OUT_DIR", root / "assets"),
                patch.object(splashes, "wiki_imageinfo", side_effect=lookup),
                patch.object(sys, "argv", ["build", "--skip-download", "--sleep", "0"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                splashes.main()
            return json.loads(out.read_text(encoding="utf-8"))

    def test_current_non_hd_art_is_selected_instead_of_archived_hd(self):
        queried = []

        def lookup(title):
            queried.append(title)
            if title == "File:Amumu_OriginalSkin.jpg":
                return {"url": "https://example.com/Amumu_OriginalSkin.jpg", "w": 1215, "h": 717}
            if "old" in title:
                return {"url": "https://example.com/Amumu_OriginalSkin_old_HD.jpg", "w": 6000, "h": 4000}
            return None

        result = self.build({}, lookup)
        self.assertEqual(result["byCid"]["32"]["wikiTitle"], "File:Amumu_OriginalSkin.jpg")
        self.assertFalse(any("old" in title for title in queried))

    def test_missing_current_art_does_not_retain_historical_cached_entry(self):
        for field, value in (
            ("url", "https://example.com/Amumu_OriginalSkin_old.jpg"),
            ("remoteUrl", "https://example.com/Amumu_OriginalSkin_old_HD.jpg"),
            ("wikiTitle", "File:Amumu OriginalSkin old.jpg"),
        ):
            with self.subTest(field=field):
                previous = {"32": {"url": "assets/draft-splash-hd/32.jpg", field: value}}
                result = self.build(previous, lambda _: None)
                self.assertNotIn("32", result["byCid"])

    def test_missing_wiki_art_keeps_current_universe_fallback(self):
        previous = {"32": {"url": "https://example.com/amumu-current.jpg", "source": "universe"}}
        result = self.build(previous, lambda _: None)
        self.assertEqual(result["byCid"]["32"], previous["32"])


if __name__ == "__main__":
    unittest.main()
