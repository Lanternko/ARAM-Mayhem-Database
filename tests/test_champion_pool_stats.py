import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tierlist_engine import build_champ_augment_picks


class ChampionPoolStatsTests(unittest.TestCase):
    def test_middle_ranked_pairs_survive_in_lazy_pool_stats(self):
        rows = [dict(champion_id=11, augment_id=aid, games=g, wins=w,
                     baseline_wr=0.5) for aid, g, w in [(1, 100, 80), (2, 100, 50),
                                                      (3, 100, 20), (4, 5, 5)]]
        result = build_champ_augment_picks(rows, {aid: {'rarity': 'kGold'} for aid in range(1, 5)}, {},
            min_games_per_pair=15, top_n=1, bot_n=1, prior_strength=20)[11]
        self.assertEqual(len(result['top']['kGold']), 1)
        self.assertEqual(len(result['bot']['kGold']), 1)
        self.assertEqual({row['augment_id'] for row in result['all']}, {1, 2, 3})
        middle = next(row for row in result['all'] if row['augment_id'] == 2)
        self.assertEqual(middle['smoothed_wr'], 0.5)
        self.assertGreater(middle['pick_rate'], 0)


if __name__ == '__main__':
    unittest.main()
