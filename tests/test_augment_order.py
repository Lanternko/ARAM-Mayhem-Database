import json
import unittest

from aram_nn.augment_order import (
    composition_context_key,
    iter_observations,
    load_role_map,
    smoothed_rate,
)


class AugmentOrderTests(unittest.TestCase):
    def test_context_is_position_free_and_side_aware(self):
        roles = load_role_map(
            {
                "1": {"tags": ["Mage"]},
                "2": {"tags": ["Tank"]},
                "3": {"tags": ["Marksman"]},
            }
        )
        self.assertEqual(
            composition_context_key([1, 2], [3], roles),
            "a:0,0,1,0,0,1|e:0,0,0,1,0,0",
        )

    def test_observations_preserve_augment_slot(self):
        roles = {1: "Mage", 2: "Tank"}
        rows = list(
            iter_observations(
                [{"teamId": 100, "championId": 1, "augments": [11, 22, 33]}],
                blue_wins=1,
                blue_champions=[1],
                red_champions=[2],
                role_map=roles,
            )
        )
        self.assertEqual([row.slot for row in rows], [1, 2, 3])
        self.assertEqual([row.augment_id for row in rows], [11, 22, 33])
        self.assertEqual([row.won for row in rows], [1, 1, 1])

    def test_smoothed_rate_shrinks_sparse_cell_to_prior(self):
        sparse = smoothed_rate(1, 1, prior_rate=0.5, prior_games=100)
        self.assertGreater(sparse, 0.5)
        self.assertLess(sparse, 0.51)
        self.assertGreater(smoothed_rate(90, 100, prior_rate=0.5, prior_games=100), 0.5)


if __name__ == "__main__":
    unittest.main()
