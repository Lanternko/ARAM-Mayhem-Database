import json
import re

from aram_nn.site.champion_analysis import analysis_html, champion_analysis
from aram_nn.site.champion_shell import champion_shell_html
from test_champion_shell_static import ROUTE, _shard, _shell_page, _write_api

RECORD = {"bayes_wr": .458, "games": 13743, "rank": 160, "rank_total": 173}


def _api_with_extras(tmp_path):
    shard = _shard()
    shard["bot"] = {"kPrismatic": [
        {"id": 5, "g": 423, "wr": 0.422},
        {"id": 6, "g": 40, "wr": 0.30},  # below the sample floor
    ]}
    shard["singleItems"] = {"popularBad": [
        {"name_zh": "星蝕", "name_en": "Eclipse", "pick": 0.32, "lift": -0.02, "items": [{"id": 6692}]},
        {"name_zh": "冷門", "name_en": "Rare", "pick": 0.04, "lift": -0.05, "items": [{"id": 6333}]},
    ]}
    api = _write_api(tmp_path, shard)
    champs = {str(i): {"late_wr": 0.5, "early_wr": 0.5 + i / 1000, "n": 500} for i in range(1, 11)}
    champs["266"] = {"late_wr": 0.52, "early_wr": 0.49, "n": 900}  # most late-leaning
    champs["999"] = {"late_wr": 0.9, "early_wr": 0.1, "n": 10}  # below min_games
    (api / "champ-empirical-axes.json").write_text(json.dumps(
        {"params": {"min_games": 400}, "champs": champs}), encoding="utf-8")
    (api / "champ-archetype-fit.json").write_text(json.dumps({
        "archetype_labels": {"dive": "Dive/衝排", "adc": "AD carry/物理後排", "poke": "Poke/消耗"},
        "champs": {"266": {"fit": {
            "dive": {"delta": 1.4, "q": True},
            "adc": {"delta": -0.9, "q": True},
            "poke": {"delta": -3.0, "q": False},  # unqualified: never cited
        }}},
    }, ensure_ascii=False), encoding="utf-8")
    return api


def test_three_languages_cover_every_paragraph(tmp_path):
    api = _api_with_extras(tmp_path)
    out = champion_analysis(route=ROUTE, record=RECORD, api_dir=api, patch_label="16.22")
    assert set(out) == {"zh-Hant", "zh-Hans", "en"}
    zh = " ".join(out["zh-Hant"]["paragraphs"])
    for needle in ("45.8%", "第 160 名", "13,743", "偏弱", "究極九頭蛇", "瘋狂科學家（42.2%",
                   "星蝕有 32%", "低 2.0 個百分點", "後期型", "第 1（", "衝排", "+1.4", "物理後排", "−0.9"):
        assert needle in zh, needle
    assert "不該出現" not in zh  # the bottom row under the sample floor
    assert "冷門" not in zh  # low pick rate
    assert "消耗" not in zh  # unqualified archetype
    en = " ".join(out["en"]["paragraphs"])
    assert "#160 of 173" in en and "Mad Scientist (42.2%" in en and "late-game champion" in en
    assert out["en"]["about"]["href"] == "/en/about/"
    assert out["zh-Hans"]["about"]["href"] == "/zh-cn/about/"
    assert "歌利亚巨人" in " ".join(out["zh-Hans"]["paragraphs"])


def test_static_page_shows_analysis_before_rankings(tmp_path):
    api = _api_with_extras(tmp_path)
    page = champion_shell_html(
        _shell_page(), route=ROUTE, record=RECORD, language="en",
        shell_url="assets/app-shell.html?v=x", loader_url="assets/champion-shell.js?v=y", api_dir=api,
    )
    assert "id='champion-analysis'" in page
    assert page.index("id='champion-fallback'") < page.index("id='champion-analysis'") < page.index("id='champion-static'")
    assert "Aatrox ARAM Mayhem analysis" in page
    assert "<a href='/en/about/'>" in page
    text = re.sub(r"<[^>]+>", " ", page[page.index("id='champion-analysis'"):page.index("id='champion-static'")])
    assert len(text.split()) > 120  # substantial prose, not a list of numbers


def test_no_data_means_no_section(tmp_path):
    api = _write_api(tmp_path, {})
    assert champion_analysis(route=ROUTE, record={}, api_dir=api, patch_label="") == {}
    assert champion_analysis(route=ROUTE, record=RECORD, api_dir=None, patch_label="") == {}
    assert analysis_html(None) == ""


def test_text_is_escaped(tmp_path):
    html = analysis_html({"title": "<b>", "paragraphs": ["a & <script>"], "about": {"label": "x", "href": "/about/"}})
    assert "<script>" not in html and "a &amp; &lt;script&gt;" in html and "&lt;b&gt;" in html
