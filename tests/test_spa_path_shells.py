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
    _retire_public_column_code,
    _site_base_href,
    _spa_deep_link_stub,
    champion_detail_base_url,
    payload_content_version,
    render_adsense_verification_tag,
    slim_site_payload,
    split_champion_detail_payloads,
    versioned_payload_url,
    write_champion_detail_shards,
    write_site_info_pages,
    write_spa_path_shells,
    SPA_FULL_SHELL_PATHS,
)


class SpaPathShellTests(unittest.TestCase):
    def test_retire_public_column_code_removes_unpublished_article_data(self) -> None:
        source = (
            "const ARTICLES = [\n"
            "  { id: 'draft-only', title: 'Private draft' },\n"
            "];\n"
            "const VIEWS = ['home', 'augments'];\n"
        )
        output = _retire_public_column_code(source)
        self.assertIn("const ARTICLES = [];", output)
        self.assertNotIn("draft-only", output)

    def test_adsense_verification_is_production_only(self) -> None:
        self.assertEqual(render_adsense_verification_tag(site_url=""), "")
        self.assertEqual(
            render_adsense_verification_tag(site_url="https://preview.example"),
            "",
        )
        tag = render_adsense_verification_tag(site_url="https://arammeta.com/")
        self.assertIn("ca-pub-8593280194977470", tag)
        self.assertIn("adsbygoogle.js", tag)
        self.assertIn("crossorigin='anonymous'", tag)

    def test_site_base_href(self) -> None:
        self.assertEqual(_site_base_href("https://arammeta.com/"), "https://arammeta.com/")
        self.assertEqual(_site_base_href("https://arammeta.com"), "https://arammeta.com/")
        self.assertEqual(_site_base_href(""), "")

    def test_deep_link_stub_stashes_path(self) -> None:
        html = _spa_deep_link_stub(
            site_url="https://arammeta.com/",
            og_image="https://arammeta.com/og-image.png",
            canonical_path="/column/how-to-read",
            title="How to read · arammeta",
            description="guide",
        )
        self.assertIn("sessionStorage.setItem('aram-spa-path'", html)
        self.assertIn("sessionStorage.setItem('aram-spa-lang','zh')", html)
        self.assertIn("location.replace('/')", html)
        self.assertIn("https://arammeta.com/column/how-to-read", html)
        self.assertIn("og:image", html)

    def test_deep_link_stub_stashes_en_lang(self) -> None:
        html = _spa_deep_link_stub(
            site_url="https://arammeta.com/",
            og_image="https://arammeta.com/og-image.png",
            canonical_path="/en/augments",
            title="Augments · arammeta",
            description="augments",
            html_lang="en",
        )
        self.assertIn("sessionStorage.setItem('aram-spa-lang','en')", html)
        self.assertIn("lang='en'", html)
        self.assertIn("/en/augments", html)

    def test_write_spa_path_shells_does_not_publish_retired_column_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.html"
            index.write_text(
                "<!doctype html><html lang='zh-Hant'><head>"
                "<title>app</title>"
                "<link rel='canonical' href='https://arammeta.com/'>"
                "<meta property='og:url' content='https://arammeta.com/'>"
                "</head><body>FULL_SPA_SHELL</body></html>",
                encoding="utf-8",
            )
            written = write_spa_path_shells(
                index,
                site_url="https://arammeta.com/",
                og_image="https://arammeta.com/og-image.png",
            )
            self.assertTrue(any(p.name == "404.html" for p in written))
            self.assertFalse((root / "column").exists())
            self.assertFalse((root / "en" / "column").exists())
            self.assertFalse((root / "zh-CN" / "column").exists())
            body_404 = (root / "404.html").read_text(encoding="utf-8")
            self.assertIn("name='robots' content='noindex'", body_404)
            self.assertIn("aram-spa-path", body_404)
            self.assertIn("location.replace('/')", body_404)
            # High-traffic locale/tab routes get the full SPA (no bounce).
            en_home = root / "en" / "index.html"
            self.assertTrue(en_home.is_file())
            en_body = en_home.read_text(encoding="utf-8")
            self.assertIn("FULL_SPA_SHELL", en_body)
            self.assertNotIn("location.replace('/')", en_body)
            self.assertIn("lang='en'", en_body)
            self.assertIn("rel='canonical' href='https://arammeta.com/en/'", en_body)
            self.assertIn(
                "hreflang='zh-Hans' href='https://arammeta.com/zh-CN/'",
                en_body,
            )
            self.assertIn(
                "hreflang='x-default' href='https://arammeta.com/'",
                en_body,
            )
            zh_cn = root / "zh-CN" / "index.html"
            zh_cn_body = zh_cn.read_text(encoding="utf-8")
            self.assertIn("FULL_SPA_SHELL", zh_cn_body)
            self.assertIn(
                "rel='canonical' href='https://arammeta.com/zh-CN/'",
                zh_cn_body,
            )
            root_body = index.read_text(encoding="utf-8")
            self.assertIn(
                "hreflang='en' href='https://arammeta.com/en/'",
                root_body,
            )
            self.assertIn("/zh-CN", SPA_FULL_SHELL_PATHS)
            self.assertIn("/game", SPA_FULL_SHELL_PATHS)
            self.assertIn("/en/game", SPA_FULL_SHELL_PATHS)
            self.assertIn("/zh-CN/game", SPA_FULL_SHELL_PATHS)
            self.assertNotIn("/column", SPA_FULL_SHELL_PATHS)
            self.assertNotIn("/en/column", SPA_FULL_SHELL_PATHS)
            self.assertNotIn("/zh-CN/column", SPA_FULL_SHELL_PATHS)

    def test_spa_navigation_emits_only_trailing_slash_directory_routes(self) -> None:
        source = (SCRIPTS / "templates" / "site.js").read_text(encoding="utf-8")
        self.assertIn("return prefix ? prefix + '/' : '/'", source)
        self.assertIn("return prefix + '/' + view + '/'", source)
        self.assertIn("const needPath = location.pathname !== wantPath", source)
        self.assertIn("pathForRoute('home') + (location.search || '')", source)

    def test_write_site_info_pages_creates_policy_pages_and_ads_txt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.html"
            index.write_text("<!doctype html><title>arammeta</title>", encoding="utf-8")
            written = write_site_info_pages(
                index,
                site_url="https://arammeta.com/",
                build_date="2026-07-15",
            )
            self.assertEqual(len(written), 4)
            privacy = (root / "privacy" / "index.html").read_text(encoding="utf-8")
            self.assertIn("Google AdSense", privacy)
            self.assertIn("Meta Pick 排行榜", privacy)
            self.assertIn("ca-pub-8593280194977470", privacy)
            self.assertIn("最後更新：2026-07-15", privacy)
            self.assertTrue((root / "about" / "index.html").is_file())
            self.assertTrue((root / "contact" / "index.html").is_file())
            self.assertEqual(
                (root / "ads.txt").read_text(encoding="utf-8"),
                "google.com, pub-8593280194977470, DIRECT, f08c47fec0942fa0\n",
            )

    def test_versioned_payload_url(self) -> None:
        self.assertEqual(
            versioned_payload_url("api/tier-list.json", "20260712"),
            "api/tier-list.json?v=20260712",
        )
        self.assertEqual(
            versioned_payload_url("api/tier-list.json?v=1", "20260712"),
            "api/tier-list.json?v=1",
        )

    def test_payload_content_version_is_stable_and_tracks_model_data(self) -> None:
        payload = {
            "detailVersion": "old-build",
            "draftModel": {"kind": "champion_lr", "coef": [0.1, -0.2]},
            "champs": {"1": {"wr": 0.51, "draftProfile": {"front": 0.4}}},
        }
        version = payload_content_version(payload)
        self.assertRegex(version, r"^[0-9a-f]{16}$")

        same_contents = dict(payload, detailVersion="another-old-build")
        self.assertEqual(payload_content_version(same_contents), version)

        changed_model = {
            **payload,
            "draftModel": {"kind": "champion_lr", "coef": [0.1, -0.3]},
        }
        self.assertNotEqual(payload_content_version(changed_model), version)

        changed_profile = {
            **payload,
            "champs": {"1": {"wr": 0.51, "draftProfile": {"front": 0.5}}},
        }
        self.assertNotEqual(payload_content_version(changed_profile), version)

    def test_champion_detail_base_url_tracks_payload_location(self) -> None:
        self.assertEqual(
            champion_detail_base_url("api/tier-list.json?v=1"),
            "api/champions",
        )
        self.assertEqual(
            champion_detail_base_url("https://cdn.example/data/tier-list.json"),
            "https://cdn.example/data/champions",
        )

    def test_split_champion_details_keeps_initial_indexes(self) -> None:
        payload = {
            "champs": {
                "1": {
                    "name": "One",
                    "top": {"kGold": [{"id": 10}]},
                    "pairs": [{"id": 2}],
                    "comp": {"front": 0.5},
                    "bot": {"kGold": [{"id": 11}]},
                    "items": {"top": [{"id": 1001}]},
                    "singleItems": {"top": [{"id": 1002}]},
                }
            }
        }
        details = split_champion_detail_payloads(payload)
        champ = payload["champs"]["1"]
        self.assertEqual(champ["name"], "One")
        self.assertIn("top", champ)
        self.assertIn("pairs", champ)
        self.assertIn("comp", champ)
        self.assertNotIn("bot", champ)
        self.assertNotIn("items", champ)
        self.assertEqual(details["1"]["items"]["top"][0]["id"], 1001)
        self.assertEqual(split_champion_detail_payloads(payload), {})

    def test_write_champion_detail_shards_sets_fetch_metadata(self) -> None:
        payload = {
            "champs": {
                "1": {
                    "name": "One",
                    "top": {},
                    "items": {"top": [{"id": 1001}]},
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "api" / "tier-list.json"
            stats = write_champion_detail_shards(
                payload,
                payload_out_path=payload_path,
                payload_url="api/tier-list.json?v=build-1",
                version="build-1",
            )
            shard = payload_path.parent / "champions" / "1.json"
            self.assertTrue(shard.is_file())
            self.assertIn('"items"', shard.read_text(encoding="utf-8"))
        self.assertEqual(stats["champs"], 1)
        self.assertGreater(stats["bytes"], 0)
        self.assertEqual(payload["detailBase"], "api/champions")
        self.assertEqual(payload["detailVersion"], "build-1")

    def test_slim_site_payload_caps_lists(self) -> None:
        payload = {
            "champs": {
                "1": {
                    "top": {"kGold": [{"id": i, "rawWr": 0.5, "wr": 0.5} for i in range(40)]},
                    "bot": {"kGold": [{"id": i, "wr": 0.4} for i in range(40)]},
                    "pairs": [{"id": i, "wr": 0.5, "g": 10, "lift": 0.0, "z": 0.0, "expected": 0.5} for i in range(80)],
                    "items": {
                        "top": [
                            {
                                "name": "甲",
                                "name_zh": "甲",
                                "name_en": "A",
                                "peerGroup": "global",
                                "peerScope": "global",
                                "g": 1,
                            }
                            for _ in range(30)
                        ]
                    },
                }
            }
        }
        slim_site_payload(payload)
        champ = payload["champs"]["1"]
        self.assertEqual(len(champ["top"]["kGold"]), 16)
        self.assertEqual(len(champ["bot"]["kGold"]), 12)
        self.assertEqual(len(champ["pairs"]), 24)
        self.assertNotIn("rawWr", champ["top"]["kGold"][0])
        item = champ["items"]["top"][0]
        self.assertNotIn("name", item)
        self.assertNotIn("peerGroup", item)
        self.assertEqual(len(champ["items"]["top"]), 16)


if __name__ == "__main__":
    unittest.main()
