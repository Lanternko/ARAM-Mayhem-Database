import unittest

from aram_nn.lcu.snowball import _adaptive_target_game_ids


def _history(queues):
    return [{"queueId": queue, "gameId": f"g{i}"} for i, queue in enumerate(queues)]


class AdaptiveHistoryTests(unittest.TestCase):
    def test_no_mayhem_skips_detail_expansion(self):
        self.assertEqual(_adaptive_target_game_ids(_history([450, 450, 450, 450, 2400]), {2400, 450}), [])

    def test_one_or_two_mayhem_only_expands_probe(self):
        history = _history([2400, 450, 450, 450, 2400, 2400])
        self.assertEqual(_adaptive_target_game_ids(history, {2400, 450}), ["g0", "g1", "g2", "g3"])

    def test_three_mayhem_expands_full_history_window(self):
        history = _history([2400, 450, 2400, 2400, 450, 2400])
        self.assertEqual(_adaptive_target_game_ids(history, {2400, 450}), [f"g{i}" for i in range(6)])

    def test_non_mayhem_crawl_falls_back_to_probe(self):
        history = _history([450, 450, 450, 450, 450])
        self.assertEqual(_adaptive_target_game_ids(history, {450}), [f"g{i}" for i in range(4)])


if __name__ == "__main__":
    unittest.main()


# --- history-expansion A/B -------------------------------------------------
# Control counts Mayhem rows only, so a player whose recent history is entirely
# 經典 (4310) scores zero and is skipped wholesale.  Treatment counts any target
# queue.  Existing tests above pass no puuid and therefore keep asserting the
# control semantics.

def _classic_history(n=6):
    return [{"gameId": f"c{i}", "queueId": 4310} for i in range(n)]


def _arm_puuid(arm):
    from aram_nn.lcu.snowball import history_arm
    for i in range(10000):
        pu = f"probe-{i}"
        if history_arm(pu) == arm:
            return pu
    raise AssertionError(f"no puuid hashed to {arm}")


def test_control_skips_pure_classic_player():
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    got = _adaptive_target_game_ids(
        _classic_history(), {450, 2400, 2450, 4310}, puuid=_arm_puuid("control")
    )
    assert got == []


def test_treatment_expands_pure_classic_player():
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    got = _adaptive_target_game_ids(
        _classic_history(), {450, 2400, 2450, 4310}, puuid=_arm_puuid("treatment")
    )
    assert len(got) == 6


def test_no_puuid_keeps_control_semantics():
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    assert _adaptive_target_game_ids(_classic_history(), {450, 2400, 4310}) == []


def test_history_and_revisit_arms_are_independent():
    from aram_nn.lcu.snowball import history_arm, revisit_arm
    ids = [f"indep-{i}" for i in range(4000)]
    agree = sum(1 for i in ids if history_arm(i) == revisit_arm(i))
    # Perfectly correlated splits would make the two concurrent experiments
    # impossible to attribute separately.
    assert 0.40 < agree / len(ids) < 0.60


def test_history_arm_is_stable_and_balanced():
    from aram_nn.lcu.snowball import history_arm
    assert history_arm("same") == history_arm("same")
    ids = [f"bal-{i}" for i in range(4000)]
    share = sum(1 for i in ids if history_arm(i) == "treatment") / len(ids)
    assert 0.45 < share < 0.55
