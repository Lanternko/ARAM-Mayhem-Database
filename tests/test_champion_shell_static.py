import json
import re
import tempfile
from pathlib import Path

from aram_nn.site.champion_shell import champion_shell_html

ROUTE = {
    "cid": "266",
    "slug": "aatrox",
    "name_zh": "厄薩斯",
    "name_en": "Aatrox",
    "name_cn": "厄薩斯",
}


def _shell_page() -> str:
    build = json.dumps({"patchLabel": "patch 26.21", "buildDate": "2026-10-26"})
    return (
        "<!doctype html><html lang='zh-Hant'><head><title>app</title>"
        "<link rel='canonical' href='https://arammeta.com/champions/aatrox/'>"
        "<meta name='description' content='厄薩斯 ARAM 大亂鬥增幅排行、出裝與召喚師技能'>"
        "<meta property='og:description' content='厄薩斯 ARAM 大亂鬥增幅排行、出裝與召喚師技能'>"
        "<meta name='twitter:description' content='厄薩斯 ARAM 大亂鬥增幅排行、出裝與召喚師技能'>"
        f"<script>window.__ARAM_BUILD__={build}</script></head>"
        "<body>FULL_SPA_SHELL</body></html>"
    )


def _shard() -> dict:
    return {
        "top": {
            "kPrismatic": [
                {"id": 1, "g": 738, "wr": 0.4895},
                {"id": 2, "g": 950, "wr": 0.4834},
                {"id": 3, "g": 612, "wr": 0.4828},
                {"id": 4, "g": 69, "wr": 0.5345},
                {"id": 5, "g": 511, "wr": 0.4778},
                {"id": 6, "g": 400, "wr": 0.4771},
            ],
            "kGold": [{"id": 10, "g": 754, "wr": 0.5063}],
            "kSilver": [],
        },
        "itemClusters": {
            "groups": [
                {"core": [{"id": 6610}, {"id": 6692}], "g": 1933, "wr": 0.4471},
                {"core": [{"id": 6333}], "g": 1700, "wr": 0.481},
            ],
        },
        "boots": {"top": [{"name_zh": "貪婪護脛", "name_en": "Gluttonous Greaves", "g": 1903, "wr": 0.4712}]},
        "spells": {"top": [{"name_zh": "閃現 + 標記", "name_en": "Flash + Mark", "g": 12477, "wr": 0.4562}]},
    }


def _write_api(root: Path, shard: dict | None = None) -> Path:
    api = root / "api"
    (api / "champions").mkdir(parents=True, exist_ok=True)
    (api / "tier-list.json").write_text(json.dumps({
        "augs": {
            "1": {"name_zh": "究極九頭蛇", "name_en": "Ultra Hydra"},
            "2": {"name_zh": "巨人", "name_en": "Goliath"},
            "3": {"name_zh": "小丑學院", "name_en": "Clown College"},
            "4": {"name_zh": "黃金撕裂", "name_en": "Gold Rend"},
            "5": {"name_zh": "瘋狂科學家", "name_en": "Mad Scientist"},
            "6": {"name_zh": "不該出現", "name_en": "Beyond Top Five"},
            "10": {"name_zh": "食魂者", "name_en": "Soul Eater"},
        },
        "itemLut": {
            "6610": {"z": "突破天際", "e": "Spear of Shojin"},
            "6692": {"z": "星蝕", "e": "Eclipse"},
            "6333": {"z": "死亡之舞", "e": "Death's Dance"},
        },
    }), encoding="utf-8")
    (api / "names-zh-cn.json").write_text(json.dumps({
        "augs": {"1": {"n": "究极九头蛇"}, "2": {"n": "歌利亚巨人"}, "3": {"n": "小丑学院"}},
        "items": {"6610": "焚天", "6692": "星蚀"},
        "t2s": {"貪": "贪", "裝": "装", "出": "出", "鞋": "鞋"},
    }, ensure_ascii=False), encoding="utf-8")
    (api / "champions" / "266.json").write_text(
        json.dumps(shard if shard is not None else _shard(), ensure_ascii=False), encoding="utf-8",
    )
    return api


def _render(api: Path, language: str = "zh-Hant") -> str:
    return champion_shell_html(
        _shell_page(), route=ROUTE, record={"bayes_wr": .458, "games": 13743}, language=language,
        shell_url="assets/app-shell.html?v=x", loader_url="assets/champion-shell.js?v=y",
        api_dir=api,
    )


def _visible_text(page: str) -> str:
    body = page[page.index("<body>"):]
    text = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def test_three_locales_carry_rankings_and_headings(tmp_path):
    api = _write_api(tmp_path)
    cases = {
        "zh-Hant": ("<h2", "究極九頭蛇", "48.9%", "738 場", "突破天際 + 星蝕", "貪婪護脛", "閃現 + 標記"),
        "zh-Hans": ("<h2", "究极九头蛇", "48.9%", "738 场", "焚天 + 星蚀", "贪婪護脛", "閃現 + 標記"),
        "en": ("<h2", "Ultra Hydra", "48.9%", "738 games", "Spear of Shojin + Eclipse",
               "Gluttonous Greaves", "Flash + Mark"),
    }
    for language, needles in cases.items():
        page = _render(api, language)
        for needle in needles:
            assert needle in page, (language, needle)
        # Only the first five ranked augments are published, in shard order.
        assert "不該出現" not in page and "Beyond Top Five" not in page
        if language == "zh-Hant":
            assert page.index("究極九頭蛇") < page.index("巨人")
        visible = _visible_text(page)
        assert "48.9%" in visible and "738" in visible
        assert len(page.encode("utf-8")) <= 30_000
        # Static block is a sibling of the fallback, never copied into the host.
        assert "id='champion-static'" in page
        assert page.index("id='champion-fallback'") < page.index("id='champion-static'")


def test_description_metas_gain_top_prismatic_names(tmp_path):
    api = _write_api(tmp_path)
    for language, expected in (
        ("zh-Hant", "究極九頭蛇、巨人、小丑學院"),
        ("zh-Hans", "究极九头蛇、歌利亚巨人、小丑学院"),
        ("en", "Ultra Hydra, Goliath, Clown College"),
    ):
        page = _render(api, language)
        metas = re.findall(r"<meta[^>]*description[^>]*>", page, flags=re.I)
        assert len(metas) == 3
        for tag in metas:
            assert expected in tag
            content = re.search(r"content='(.*?)'", tag)
            assert content and len(content[1]) <= 160


def test_empty_data_omits_whole_sections(tmp_path):
    shard = {
        "top": {"kPrismatic": [], "kGold": [], "kSilver": []},
        "itemClusters": {"groups": []},
        "boots": {"top": []},
        "spells": {"top": []},
    }
    api = _write_api(tmp_path, shard)
    page = _render(api)
    assert "id='champion-static'" not in page
    assert "<h2" not in page
    # No injected summary either, and the snapshot copy is still there.
    assert "正在載入增幅與出裝…" in page
    # Missing descriptions keep their original text (no names to append).
    assert "厄薩斯 ARAM 大亂鬥增幅排行、出裝與召喚師技能'" in page


def test_missing_shard_keeps_page_intact(tmp_path):
    api = _write_api(tmp_path)
    (api / "champions" / "266.json").unlink()
    page = _render(api)
    assert "id='champion-static'" not in page
    assert "champion-shell.js?v=y" in page


def test_names_are_html_escaped(tmp_path):
    shard = _shard()
    api = _write_api(tmp_path, shard)
    tier = json.loads((api / "tier-list.json").read_text(encoding="utf-8"))
    tier["augs"]["1"]["name_zh"] = "A & B <script>alert(1)</script>"
    (api / "tier-list.json").write_text(json.dumps(tier, ensure_ascii=False), encoding="utf-8")
    page = _render(api)
    assert "A &amp; B &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    metas = re.findall(r"<meta[^>]*description[^>]*>", page, flags=re.I)
    assert all("&lt;script&gt;" in tag for tag in metas)


def test_no_api_dir_keeps_previous_shell_contract():
    page = champion_shell_html(
        _shell_page(), route=ROUTE, record={"bayes_wr": .458, "games": 13743},
        language="zh-Hant", shell_url="assets/app-shell.html?v=x",
        loader_url="assets/champion-shell.js?v=y",
    )
    assert "id='champion-static'" not in page
    assert "id='champion-fallback'" in page
    assert "53.0%" not in page and "45.8%" in page
