"""Tests for the dependency-free rank curve (bot.ranking).

NOTE: unlike the other test modules, this one sets NO env shim and never imports
bot.config — that is the whole point of bot.ranking living on its own. If this
file ever needs the WALLET_PRIVATE_KEY shim to import, the key-less web server
can no longer use the curve, and that regression must be caught here.
"""
from __future__ import annotations

import unittest

from bot import ranking


class TestRankWeights(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(ranking.rank_weights({}), {})
        self.assertEqual(ranking.ranked_holders({}), [])

    def test_only_rank_matters_not_magnitude(self):
        self.assertEqual(
            ranking.rank_weights({"a": 1_000_000, "b": 2}),
            ranking.rank_weights({"a": 5, "b": 4}),
        )

    def test_ties_get_equal_weight(self):
        w = ranking.rank_weights({"a": 100, "b": 100, "c": 50})
        self.assertEqual(w["a"], w["b"])
        self.assertGreater(w["a"], w["c"])


class TestRankedHolders(unittest.TestCase):
    def test_order_and_rank_numbers(self):
        rows = ranking.ranked_holders({"a": 300, "b": 200, "c": 100})
        self.assertEqual([r["wallet"] for r in rows], ["a", "b", "c"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertGreater(rows[0]["share_bps"], rows[1]["share_bps"])
        self.assertGreater(rows[1]["share_bps"], rows[2]["share_bps"])

    def test_ties_share_rank_and_share(self):
        rows = ranking.ranked_holders({"a": 100, "b": 100, "c": 50})
        by = {r["wallet"]: r for r in rows}
        self.assertEqual(by["a"]["rank"], by["b"]["rank"])   # competition ranking: 1,1,3
        self.assertEqual(by["a"]["rank"], 1)
        self.assertEqual(by["c"]["rank"], 3)
        self.assertEqual(by["a"]["share_bps"], by["b"]["share_bps"])
        self.assertGreater(by["a"]["share_bps"], by["c"]["share_bps"])

    def test_drops_nonpositive_held(self):
        rows = ranking.ranked_holders({"a": 100, "b": 0, "c": -5})
        self.assertEqual([r["wallet"] for r in rows], ["a"])

    def test_share_bps_never_exceeds_pool(self):
        held = {f"w{i}": (i + 1) * 10 for i in range(50)}
        rows = ranking.ranked_holders(held)
        total = sum(r["share_bps"] for r in rows)
        self.assertLessEqual(total, 10_000)   # floor split, never over 100%
        self.assertGreater(total, 9_900)      # accounts for ~all of the pool

    def test_top_is_biggest_even_with_one_second_lead(self):
        rows = ranking.ranked_holders({"leader": 101, "x": 100, "y": 100})
        self.assertEqual(rows[0]["wallet"], "leader")
        self.assertEqual(rows[0]["rank"], 1)
        self.assertGreater(rows[0]["share_bps"], rows[1]["share_bps"])


if __name__ == "__main__":
    unittest.main()
