from scripts import tierlist_engine as engine


class _FakeResponse:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def raise_for_status(self) -> None:
        pass

    def json(self) -> list[dict]:
        return self._rows


def test_live_augment_metadata_carries_no_removed_mayhem_sets(monkeypatch) -> None:
    # Mayhem augment sets were removed in 16.12. An augment that belonged to
    # "Make it Rain" in 16.11 must not keep that label on current cards.
    rows = [{"id": 1386, "nameTRA": "Upgrade: Collector", "rarity": "kGold"}]
    monkeypatch.setattr(engine, "load_augment_display_tags", lambda _cache: {})
    monkeypatch.setattr(engine.httpx, "get", lambda *_a, **_k: _FakeResponse(rows))

    meta = engine.load_augment_metadata()[1386]

    assert meta["sets"] == []
    assert meta["set"] == meta["set_zh"] == meta["set_en"] == meta["setSlug"] == ""
    assert "economy" not in engine.augment_type_slugs(
        {**meta, "name": "", "name_zh": "", "name_en": ""}
    )


def test_legacy_lookup_lists_each_set_once_per_augment() -> None:
    lookup = engine._legacy_augment_set_lookup()

    infos = lookup[engine._normalize_augment_name("Upgrade: Collector")]

    assert [info["slug"] for info in infos] == ["make-it-rain"]
    assert engine.LEGACY_MAYHEM_AUGMENT_SETS_LAST_PATCH == "16.11"
