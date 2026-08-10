import json

from aram_nn.lcu.snowball import _merge_queue_counts


def test_first_visit_starts_from_empty():
    out = json.loads(_merge_queue_counts(None, {2400: 3, 4310: 1}))
    assert out == {"2400": 3, "4310": 1}


def test_accumulates_across_visits():
    first = _merge_queue_counts(None, {2400: 2})
    second = _merge_queue_counts(first, {2400: 1, 450: 4})
    assert json.loads(second) == {"2400": 3, "450": 4}


def test_zero_yield_visit_leaves_blob_untouched():
    stored = _merge_queue_counts(None, {2400: 2})
    assert _merge_queue_counts(stored, {}) == stored
    assert _merge_queue_counts(None, None) is None


def test_corrupt_blob_restarts_instead_of_raising():
    # Losing one player's attribution history must not stall the crawl.
    assert json.loads(_merge_queue_counts("not json", {450: 1})) == {"450": 1}
    assert json.loads(_merge_queue_counts("[1,2]", {450: 1})) == {"450": 1}
