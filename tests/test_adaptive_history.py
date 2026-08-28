import unittest

from aram_nn.lcu.snowball import _adaptive_target_game_ids


def _history(queues):
    return [{"queueId": queue, "gameId": f"g{i}"} for i, queue in enumerate(queues)]


def _versioned_history(rows):
    return [
        {"queueId": queue, "gameId": f"g{i}", "gameVersion": version}
        for i, (queue, version) in enumerate(rows)
    ]


class AdaptiveHistoryTests(unittest.TestCase):
    def test_three_target_games_expand_full_history(self):
        self.assertEqual(_adaptive_target_game_ids(_history([450, 450, 450, 450, 2400]), {2400, 450}), [f"g{i}" for i in range(5)])

    def test_one_or_two_target_games_only_expands_probe(self):
        history = _history([2400, 420, 450, 420, 2400, 2400])
        self.assertEqual(_adaptive_target_game_ids(history, {2400, 450}), ["g0", "g2"])

    def test_three_mayhem_expands_full_history_window(self):
        history = _history([2400, 450, 2400, 2400, 450, 2400])
        self.assertEqual(_adaptive_target_game_ids(history, {2400, 450}), [f"g{i}" for i in range(6)])

    def test_dense_non_mayhem_target_crawl_expands_full_window(self):
        history = _history([450, 450, 450, 450, 450])
        self.assertEqual(_adaptive_target_game_ids(history, {450}), [f"g{i}" for i in range(5)])

    def test_jade_on_current_patch_makes_player_active(self):
        history = _versioned_history(
            [(4310, "16.15.1"), (420, "16.15.1"), (420, "16.15.1"), (420, "16.15.1")]
        )
        self.assertEqual(
            _adaptive_target_game_ids(history, {4310}, current_patch="16.15.8024387"),
            ["g0"],
        )

    def test_old_patch_targets_do_not_make_player_active(self):
        history = _versioned_history(
            [(4310, "16.14.9"), (2400, "16.14.9"), (450, "16.14.9"), (420, "16.15.1")]
        )
        self.assertEqual(
            _adaptive_target_game_ids(history, {450, 2400, 4310}, current_patch="16.15"),
            [],
        )

    def test_full_expansion_keeps_only_current_patch_targets(self):
        history = _versioned_history(
            [
                (4310, "16.15.1"),
                (2400, "16.15.2"),
                (450, "16.15.3"),
                (420, "16.15.4"),
                (4310, "16.14.9"),
                (4310, "16.15.5"),
            ]
        )
        self.assertEqual(
            _adaptive_target_game_ids(
                history, {450, 2400, 4310}, current_patch="16.15"
            ),
            ["g0", "g1", "g2", "g5"],
        )


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


def test_retired_control_arm_no_longer_skips_pure_classic_player():
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    got = _adaptive_target_game_ids(
        _classic_history(), {450, 2400, 2450, 4310}, puuid=_arm_puuid("control")
    )
    assert len(got) == 6


def test_retired_probe_arm_uses_the_shipped_full_policy():
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    got = _adaptive_target_game_ids(
        _classic_history(20), {450, 2400, 2450, 4310}, puuid=_arm_puuid("probe")
    )
    assert len(got) == 20


def test_full_arm_expands_classic_history_completely():
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    got = _adaptive_target_game_ids(
        _classic_history(20), {450, 2400, 2450, 4310}, puuid=_arm_puuid("full")
    )
    assert len(got) == 20


def test_all_arms_skip_traditional_rift_players():
    # 400/420/430/440 are not target queues, so they never make a player visible.
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    rift = [{"gameId": f"s{i}", "queueId": 420} for i in range(6)]
    for arm in ("control", "probe", "full"):
        assert _adaptive_target_game_ids(
            rift, {450, 2400, 2450, 4310}, puuid=_arm_puuid(arm)
        ) == []


def test_mayhem_heavy_player_is_identical_across_arms():
    # The main data stream must not be perturbed by the experiment.
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    hist = [{"gameId": f"m{i}", "queueId": 2400} for i in range(3)]
    hist += [{"gameId": f"j{i}", "queueId": 4310} for i in range(10)]
    sizes = {
        len(_adaptive_target_game_ids(hist, {450, 2400, 2450, 4310}, puuid=_arm_puuid(a)))
        for a in ("control", "probe", "full")
    }
    assert len(sizes) == 1


def test_no_puuid_uses_shipped_full_semantics():
    from aram_nn.lcu.snowball import _adaptive_target_game_ids
    assert len(_adaptive_target_game_ids(_classic_history(), {450, 2400, 4310})) == 6


def test_history_and_revisit_arms_are_independent():
    from aram_nn.lcu.snowball import history_arm, revisit_arm
    ids = [f"indep-{i}" for i in range(6000)]
    # Treatment-vs-control agreement across the two splits; correlated splits
    # would make the concurrent experiments impossible to attribute separately.
    agree = sum(
        1 for i in ids
        if (history_arm(i) != "control") == (revisit_arm(i) == "treatment")
    )
    assert 0.40 < agree / len(ids) < 0.60


def test_history_arm_is_stable_and_balanced():
    from aram_nn.lcu.snowball import history_arm
    assert history_arm("same") == history_arm("same")
    ids = [f"bal-{i}" for i in range(9000)]
    for arm in ("control", "probe", "full"):
        share = sum(1 for i in ids if history_arm(i) == arm) / len(ids)
        assert 0.28 < share < 0.39, (arm, share)
