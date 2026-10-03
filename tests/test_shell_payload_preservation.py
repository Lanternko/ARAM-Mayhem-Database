"""Shell rebuilds must preserve the complete published data snapshot."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tierlist_render as render


class ShellPayloadPreservationTests(unittest.TestCase):
    def test_shell_build_does_not_read_model_or_rewrite_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            api = out / "api"
            (api / "champions").mkdir(parents=True)
            snapshot = json.loads((ROOT / "docs/api/tier-list.json").read_text(encoding="utf-8"))
            (api / "tier-list.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            (api / "champions/1.json").write_bytes(b'{"sentinel":true}\n')
            before = {p.relative_to(api): p.read_bytes() for p in api.rglob("*") if p.is_file()}
            with patch.object(render, "load_draft_composition_lr_payload", side_effect=AssertionError("local model read")), patch.object(render, "hydrate_draft_champion_profiles", side_effect=AssertionError("profile recompute")), patch.object(render, "write_champion_detail_shards", side_effect=AssertionError("shard write")):
                render._run_shell_only(
                    out_path=out / "index.html", db=out / "missing.db",
                    queue_id=2400, patch_prefix="wrong-local-patch", payload_out=None,
                    payload_url="api/tier-list.json", site_url="https://arammeta.com/",
                    og_image="", build_date="2026-09-12", cloudflare_analytics_token="",
                    ga_measurement_id="", min_pair_games=5, min_synergy_games=5,
                )
            self.assertEqual(before, {p.relative_to(api): p.read_bytes() for p in api.rglob("*") if p.is_file()})
            from xml.etree import ElementTree as ET
            from aram_nn.site.search_discovery import SITEMAP_NS
            locations = {
                node.text for node in ET.parse(out / "sitemap.xml").findall(f".//{{{SITEMAP_NS}}}loc")
            }
            self.assertIn("https://arammeta.com/augments/pools/", locations)
            self.assertIn("https://arammeta.com/champions/ahri/", locations)
            self.assertNotIn("https://arammeta.com/p/player-history/", locations)
            self.assertTrue((out / "robots.txt").exists())
            index_html = (out / "index.html").read_text(encoding="utf-8")
            site_js = (out / "assets/site.js").read_text(encoding="utf-8")
            payload_ref = f"api/tier-list.json?v={snapshot['detailVersion']}"
            # The preload and the app's fetch must name the same URL, and the
            # build-varying URL lives in the shell, not the shared script.
            self.assertIn(f"<link rel='preload' href='{payload_ref}'", index_html)
            self.assertIn(f'"payload":"{payload_ref}"', index_html)
            self.assertNotIn(snapshot["detailVersion"], site_js)
            self.assertNotIn("2026-09-12", site_js)


if __name__ == "__main__":
    unittest.main()
