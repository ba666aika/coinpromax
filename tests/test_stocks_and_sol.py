"""Tests for the two new CPM money legs.

bot.stocks.buy_basket — even split across the basket, re-capped, leg-isolated.
bot.distribute.distribute_sol — pays only the given pool, dust-floored,
DRY_RUN-safe. All network mocked.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from solders.keypair import Keypair as _TestKp  # noqa: E402

_test_kp = _TestKp()
os.environ.setdefault("HELIUS_API_KEY", "test")
os.environ.setdefault("WALLET_PRIVATE_KEY", str(_test_kp))
os.environ.setdefault("LOYALTY_MINT", "2jCt3hj9vd7YpV7Sr3VA5nk3tdSpJtZezeoJXW4Xpump")
os.environ.setdefault("OPERATOR_WALLET", str(_TestKp().pubkey()))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("MIN_HOLDING_RAW", "1")

from bot import config  # noqa: E402
from bot import distribute as dist  # noqa: E402
from bot import stocks  # noqa: E402
from bot.rpc import RPCError  # noqa: E402


def _wallet() -> str:
    return str(_TestKp().pubkey())


class TestBuyBasket(unittest.TestCase):
    def _mocks(self):
        return (
            mock.patch.object(stocks, "_quote", return_value={"outAmount": "1"}),
            mock.patch.object(stocks, "_swap_tx", return_value=b"tx"),
            mock.patch.object(stocks, "_sign_and_send", return_value="sig"),
        )

    def test_even_split_across_basket(self):
        q, s, send = self._mocks()
        with q as mq, s, send as msend:
            sigs = stocks.buy_basket(5_000_000)
        n = len(config.STOCK_MINTS)
        self.assertEqual(len(sigs), n)
        per_leg = 5_000_000 // n
        for call in mq.call_args_list:
            self.assertEqual(call.args[2], per_leg)  # every leg gets the even share

    def test_recap_defense_in_depth(self):
        q, s, send = self._mocks()
        with mock.patch.object(config, "MAX_STOCK_BASKET_LAMPORTS", 1_000):
            with q as mq, s, send:
                stocks.buy_basket(999_999_999)  # caller "forgot" the cap
        per_leg = 1_000 // len(config.STOCK_MINTS)
        for call in mq.call_args_list:
            self.assertEqual(call.args[2], per_leg)

    def test_failed_leg_is_isolated(self):
        calls = {"n": 0}

        def quote_fail_second(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RPCError("quote down")
            return {"outAmount": "1"}

        with mock.patch.object(stocks, "_quote", side_effect=quote_fail_second), \
             mock.patch.object(stocks, "_swap_tx", return_value=b"tx"), \
             mock.patch.object(stocks, "_sign_and_send", return_value="sig"):
            sigs = stocks.buy_basket(5_000_000)
        # One leg failed → its SOL stays put; the other legs still bought.
        self.assertEqual(len(sigs), len(config.STOCK_MINTS) - 1)

    def test_zero_and_dust_budgets_buy_nothing(self):
        q, s, send = self._mocks()
        with q as mq, s, send:
            self.assertEqual(stocks.buy_basket(0), [])
            self.assertEqual(stocks.buy_basket(len(config.STOCK_MINTS) - 1), [])  # per-leg floors to 0
        mq.assert_not_called()

    def test_dry_run_signs_nothing(self):
        q, s, send = self._mocks()
        with mock.patch.object(config, "DRY_RUN", True):
            with q as mq, s, send as msend:
                self.assertEqual(stocks.buy_basket(5_000_000), [])
        mq.assert_not_called()
        msend.assert_not_called()


class TestDistributeSol(unittest.TestCase):
    def test_never_pays_more_than_pool_and_floors_dust(self):
        a, b, c = _wallet(), _wallet(), _wallet()
        weights = {a: 600, b: 300, c: 1}  # c's floor share is dust
        with mock.patch.object(config, "MIN_SOL_PAYOUT_LAMPORTS", 200_000), \
             mock.patch.object(dist.rpc, "get_recent_blockhash", return_value="bh"), \
             mock.patch.object(dist, "_send_with_backoff", return_value="sig") as msend, \
             mock.patch.object(dist, "_confirm", side_effect=lambda sigs: set(sigs)), \
             mock.patch.object(dist.stx, "priority_fee_ixs", return_value=[]), \
             mock.patch.object(dist.stx, "ix_transfer_sol", return_value=object()), \
             mock.patch.object(dist.stx, "build_tx_b64", return_value="b64"):
            res = dist.distribute_sol(1_000_000, weights)
        # c's share (1/901 of pool ≈ 1109) is below the 200k floor → skipped.
        self.assertEqual(res["recipients"], 2)
        self.assertEqual(msend.call_count, 2)
        self.assertLessEqual(res["actual_sent"], 1_000_000)
        self.assertGreater(res["actual_sent"], 0)

    def test_empty_pool_or_weights_pays_nothing(self):
        with mock.patch.object(dist, "_send_with_backoff") as msend:
            self.assertEqual(dist.distribute_sol(0, {_wallet(): 10})["actual_sent"], 0)
            self.assertEqual(dist.distribute_sol(1_000_000, {})["actual_sent"], 0)
        msend.assert_not_called()

    def test_dry_run_sends_nothing(self):
        with mock.patch.object(config, "DRY_RUN", True), \
             mock.patch.object(config, "MIN_SOL_PAYOUT_LAMPORTS", 1), \
             mock.patch.object(dist, "_send_with_backoff") as msend:
            res = dist.distribute_sol(1_000_000, {_wallet(): 10})
        self.assertTrue(res.get("dry_run"))
        self.assertEqual(res["actual_sent"], 0)
        msend.assert_not_called()

    def test_unconfirmed_sends_do_not_count_as_sent(self):
        a = _wallet()
        with mock.patch.object(config, "MIN_SOL_PAYOUT_LAMPORTS", 1), \
             mock.patch.object(dist.rpc, "get_recent_blockhash", return_value="bh"), \
             mock.patch.object(dist, "_send_with_backoff", return_value="sig"), \
             mock.patch.object(dist, "_confirm", return_value=set()), \
             mock.patch.object(dist.stx, "priority_fee_ixs", return_value=[]), \
             mock.patch.object(dist.stx, "ix_transfer_sol", return_value=object()), \
             mock.patch.object(dist.stx, "build_tx_b64", return_value="b64"):
            res = dist.distribute_sol(1_000_000, {a: 10})
        # Sent but never confirmed → conservative accounting: actual_sent = 0,
        # the caller does NOT decrement the pool (self-heals next window).
        self.assertEqual(res["actual_sent"], 0)


if __name__ == "__main__":
    unittest.main()
