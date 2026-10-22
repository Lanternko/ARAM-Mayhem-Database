from aram_nn.site.augment_strings import augment_description_text, stringtable_text
from scripts import tierlist_engine as engine


def test_new_augment_uses_current_rst_hash():
    # Actual 26.21 key/hash pair; the translation exists under an unreversed key.
    entries = {"{23088a1d2d}": "施放技能後，下一次普攻更快。"}
    assert stringtable_text(entries, "Kiwi_Augment_TheBoomAndTheBass_Description") == (
        "施放技能後，下一次普攻更快。"
    )


def test_readable_key_keeps_priority():
    entries = {"kiwi_augment_theboomandthebass_description": "readable", "{23088a1d2d}": "hashed"}
    assert stringtable_text(entries, "Kiwi_Augment_TheBoomAndTheBass_Description") == "readable"
    assert stringtable_text(entries, "missing") == ""


def test_nested_hashed_references_and_dynamic_spell_name():
    entries = {
        "description": "<evolve>強化</evolve>{{SpellName}}！ {{Kiwi_GoH_PentakillAug_Generic_Tooltip}}",
        "{1a84b84f84}": "{{ SpellName }}獲得20技能加速。",
        "dance": "{{Kiwi_Augment_Totentanz_Summoner_Name}}",
        "{1d9edfe207}": "與死神共舞",
    }
    assert augment_description_text(entries, "description", locale="zh_tw") == (
        "<evolve>強化</evolve>對應技能！ 對應技能獲得20技能加速。"
    )
    assert augment_description_text({"a": "{{SpellName}}"}, "a", locale="en_us") == "the affected ability"
    assert augment_description_text(entries, "dance", locale="zh_tw") == "與死神共舞"


def test_reference_cycle_and_missing_reference_are_bounded():
    entries = {"a": "start {{b}}", "b": "{{a}} {{unknown}}"}
    text = augment_description_text(entries, "a", locale="en_us")
    assert text.startswith("start ")
    assert "{{" not in text
    assert len(text) < 30


def test_loader_resolves_hashes_and_falls_back_to_tooltip(monkeypatch, tmp_path):
    kiwi = {
        "one": {"__type": "AugmentData", "AugmentPlatformId": 3078,
                "DescriptionTra": "Kiwi_Augment_TheBoomAndTheBass_Description"},
        "two": {"__type": "AugmentData", "AugmentPlatformId": 3079,
                "DescriptionTra": "not_available", "AugmentTooltipTra": "dance"},
    }
    table = {"entries": {
        "{23088a1d2d}": "<speed>更快</speed><br>造成@Damage@傷害。",
        "dance": "{{Kiwi_Augment_Totentanz_Summoner_Name}}",
        "kiwi_augment_totentanz_summoner_name": "與死神共舞",
    }}
    monkeypatch.setattr(engine, "_cached_get_json", lambda url, *a, **kw: kiwi if "kiwi.bin" in url else table)
    assert engine.load_augment_descriptions(tmp_path, locale="zh_tw", cache_name="strings.json") == {
        3078: "更快 造成[數值]傷害。", 3079: "與死神共舞",
    }
