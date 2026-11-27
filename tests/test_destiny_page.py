import json

from aram_nn.site.destiny_page import write_destiny_pages


def _payload():
    return {
        "patch_prefix": "16.23", "ddv": "16.23.1",
        "augs": {"3081": {"icon": "assets/icons/destiny.png"}},
        "itemLut": {"3089": {"e": "Rabadon's Deathcap", "z": "死亡之帽"}, "3032": {"e": "Yun Tal", "z": "芮蘭颶風箭"}},
        "champs": {
            "10": {"name_zh": "凱爾", "name_en": "Kayle", "alias": "Kayle",
                   "destinyItem": {"id": 3032, "lift": 0.066, "alt": {"id": 3089, "lift": 0.05}}},
            "1": {"name_zh": "安妮", "name_en": "Annie", "alias": "Annie",
                  "destinyItem": {"id": 3089, "lift": 0.09, "prev": True}},
            "2": {"name_zh": "歐拉夫", "name_en": "Olaf", "alias": "Olaf"},
        },
    }


def test_destiny_pages_list_each_champion_in_three_locales(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "tier-list.json").write_text(json.dumps(_payload()), encoding="utf-8")
    (tmp_path / "api" / "names-zh-cn.json").write_text(
        json.dumps({"champs": {"10": "凯尔"}, "items": {"3089": "灭世者的死亡之帽"}}), encoding="utf-8")
    (tmp_path / "champions" / "kayle").mkdir(parents=True)

    pages = write_destiny_pages(tmp_path)

    assert len(pages) == 3
    zh = (tmp_path / "augments/weapon-of-destiny/index.html").read_text(encoding="utf-8")
    assert "<h1>命定武器推薦裝備</h1>" in zh
    assert zh.count("class='dw-row'") == 2 and "歐拉夫" not in zh
    assert zh.index("安妮") < zh.index("凱爾")  # strongest gain first
    assert "+6.6%" in zh and "+5.0%" in zh and "上版資料" in zh
    assert "href='/champions/kayle/'" in zh and "/champions/annie/" not in zh
    assert "版本 26.23" in zh
    assert "href='https://arammeta.com/augments/weapon-of-destiny/'" in zh
    assert zh.count("rel='alternate'") == 4 and "noindex" not in zh
    cn = (tmp_path / "zh-cn/augments/weapon-of-destiny/index.html").read_text(encoding="utf-8")
    assert "<html lang='zh-Hans'>" in cn and "凯尔" in cn and "灭世者的死亡之帽" in cn
    assert "href='/zh-cn/champions/kayle/'" in cn
    en = (tmp_path / "en/augments/weapon-of-destiny/index.html").read_text(encoding="utf-8")
    assert "Rabadon&#x27;s Deathcap" in en and "Kayle" in en


def test_destiny_pages_skip_without_payload(tmp_path):
    assert write_destiny_pages(tmp_path) == []
