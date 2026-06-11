"""Tests for the distribution layer.

Two halves:
  * `compute_payouts` — PURE integer math. The money-safety invariants
    (never over-distribute, never overpay a holder, leftover >= 0) are proven
    here exhaustively, including a 10k-holder stress run.
  * `distribute` I/O accounting — with rpc send/confirm mocked, proves that
    only CONFIRMED transfers are counted as `actual_sent`, that chunking +
    bounded concurrency don't corrupt the tally, and that the payouts log
    matches what was sent.
"""
from __future__ import annotations

import math
import os
import random
import tempfile
import unittest
from itertools import count
from unittest import mock

from solders.keypair import Keypair as _TestKp  # noqa: E402

# Dummy env so `import bot.config` doesn't sys.exit during collection.
_test_kp = _TestKp()
os.environ.setdefault("HELIUS_API_KEY", "test")
os.environ.setdefault("WALLET_PRIVATE_KEY", str(_test_kp))
os.environ.setdefault("LOYALTY_MINT", "2jCt3hj9vd7YpV7Sr3VA5nk3tdSpJtZezeoJXW4Xpump")
os.environ.setdefault("OPERATOR_WALLET", str(_TestKp().pubkey()))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("MIN_HOLDING_RAW", "1")

from bot import config  # noqa: E402
from bot import distribute as dist  # noqa: E402


def _wallets(n: int) -> list[str]:
    """n distinct, real base58 pubkeys (so the I/O layer can build real txs)."""
    return [str(_TestKp().pubkey()) for _ in range(n)]


class TestRankWeights(unittest.TestCase):
    """The rank curve: share depends on POSITION (longest=#1), not on absolute
    held_seconds; equal held ⇒ equal payout; ties don't inflate the curve."""

    def test_empty(self):
        self.assertEqual(dist.rank_weights({}), {})

    def test_zero_and_negative_held_dropped(self):
        self.assertEqual(dist.rank_weights({"a": 0, "b": -5}), {})

    def test_only_rank_matters_not_magnitude(self):
        # Same ordering ⇒ identical weights, regardless of the absolute gap.
        big = dist.rank_weights({"a": 1_000_000, "b": 2})
        small = dist.rank_weights({"a": 5, "b": 4})
        self.assertEqual(big, small)

    def test_longest_holder_is_top_even_by_one_second(self):
        w = dist.rank_weights({"leader": 101, "rest": 100})
        self.assertGreater(w["leader"], w["rest"])
        # A 1-second lead gives the SAME advantage as a huge lead (rank, not size).
        w2 = dist.rank_weights({"leader": 10_000, "rest": 100})
        self.assertEqual(w, w2)

    def test_sqrt_curve_ratio(self):
        # rank1 : rank2 : rank4 weights ≈ 1 : 1/√2 : 1/2
        w = dist.rank_weights({"a": 40, "b": 30, "c": 20, "d": 10})  # ranks 1..4
        self.assertAlmostEqual(w["a"] / w["b"], math.sqrt(2), places=3)
        self.assertAlmostEqual(w["a"] / w["d"], 2.0, places=3)

    def test_ties_get_equal_weight(self):
        w = dist.rank_weights({"a": 100, "b": 100, "c": 50})
        self.assertEqual(w["a"], w["b"])      # equal held ⇒ equal payout
        self.assertGreater(w["a"], w["c"])

    def test_ties_preserve_curve_total(self):
        # Two wallets tied at the top occupy ranks 1&2; their combined weight
        # equals rank1+rank2 of the untied curve (no inflation), within rounding.
        tied = dist.rank_weights({"a": 100, "b": 100, "c": 50})
        untied = dist.rank_weights({"a": 200, "b": 100, "c": 50})
        self.assertAlmostEqual(tied["a"] + tied["b"], untied["a"] + untied["b"], delta=2)
        self.assertEqual(tied["c"], untied["c"])

    def test_integration_rank_then_payout(self):
        held = {"early": 10_000, "mid": 5_000, "tie1": 1_000, "tie2": 1_000}
        weights = dist.rank_weights(held)
        payouts, leftover = dist.compute_payouts(1_000_000, weights)
        d = dict(payouts)
        self.assertEqual(max(d, key=d.get), "early")   # longest holder paid most
        self.assertEqual(d["tie1"], d["tie2"])          # equal held ⇒ equal pay
        self.assertGreater(d["mid"], d["tie1"])
        self.assertGreaterEqual(leftover, 0)
        self.assertEqual(sum(d.values()) + leftover, 1_000_000)  # never over-distribute


class TestComputePayoutsInvariants(unittest.TestCase):
    def test_empty_weights(self):
        payouts, leftover = dist.compute_payouts(1000, {})
        self.assertEqual(payouts, [])
        self.assertEqual(leftover, 1000)

    def test_zero_pool(self):
        payouts, leftover = dist.compute_payouts(0, {"a": 5})
        self.assertEqual(payouts, [])
        self.assertEqual(leftover, 0)

    def test_negative_pool_clamped(self):
        payouts, leftover = dist.compute_payouts(-50, {"a": 5})
        self.assertEqual(payouts, [])
        self.assertEqual(leftover, 0)

    def test_zero_and_negative_weights_skipped(self):
        payouts, leftover = dist.compute_payouts(100, {"a": 0, "b": -3, "c": 10})
        self.assertEqual(payouts, [("c", 100)])
        self.assertEqual(leftover, 0)

    def test_floor_split_keeps_remainder_as_leftover(self):
        payouts, leftover = dist.compute_payouts(10, {"a": 1, "b": 1, "c": 1})
        self.assertEqual(dict(payouts), {"a": 3, "b": 3, "c": 3})
        self.assertEqual(leftover, 1)
        self.assertEqual(sum(a for _, a in payouts) + leftover, 10)

    def test_deterministic_order_desc_weight_then_wallet(self):
        weights = {"z": 5, "a": 5, "m": 10}
        payouts, _ = dist.compute_payouts(1000, weights)
        # Highest weight first; ties broken by wallet string ascending.
        self.assertEqual([w for w, _ in payouts], ["m", "a", "z"])

    def test_sum_never_exceeds_pool_proportional(self):
        weights = {"a": 1, "b": 2, "c": 3, "d": 4}
        pool = 999
        payouts, leftover = dist.compute_payouts(pool, weights)
        self.assertLessEqual(sum(a for _, a in payouts), pool)
        self.assertGreaterEqual(leftover, 0)
        self.assertEqual(sum(a for _, a in payouts) + leftover, pool)

    def test_no_holder_is_overpaid(self):
        """Every amount <= its exact real share pool*w/total_w (integer check)."""
        weights = {"a": 7, "b": 11, "c": 13, "d": 17}
        total_w = sum(weights.values())
        pool = 1_000_003
        payouts, _ = dist.compute_payouts(pool, weights)
        for wallet, amt in payouts:
            # amt <= pool*w/total_w  ⇔  amt*total_w <= pool*w  (no floats)
            self.assertLessEqual(amt * total_w, pool * weights[wallet])

    def test_single_holder_takes_whole_pool(self):
        payouts, leftover = dist.compute_payouts(777, {"solo": 42})
        self.assertEqual(payouts, [("solo", 777)])
        self.assertEqual(leftover, 0)

    def test_determinism_repeat(self):
        weights = {f"w{i}": (i % 9) + 1 for i in range(200)}
        a = dist.compute_payouts(123_456_789, weights)
        b = dist.compute_payouts(123_456_789, weights)
        self.assertEqual(a, b)


class TestComputePayoutsStress(unittest.TestCase):
    def test_10k_holders_invariants_hold(self):
        rng = random.Random(1337)
        weights = {f"holder_{i}": rng.randint(1, 1_000_000) for i in range(10_000)}
        pool = 4_321_000_000_000  # ~4321 $LOYALTY at 9 decimals — large
        total_w = sum(weights.values())

        payouts, leftover = dist.compute_payouts(pool, weights)

        distributed = sum(a for _, a in payouts)
        # 1. Never over-distribute.
        self.assertLessEqual(distributed, pool)
        # 2. Leftover is exact and non-negative.
        self.assertEqual(distributed + leftover, pool)
        self.assertGreaterEqual(leftover, 0)
        # 3. Floor truncation loses < 1 unit per holder, so leftover < count.
        self.assertLess(leftover, len(weights))
        # 4. Every amount positive and no overpay.
        for wallet, amt in payouts:
            self.assertGreater(amt, 0)
            self.assertLessEqual(amt * total_w, pool * weights[wallet])
        # 5. Output sorted by (-weight, wallet) — deterministic.
        keys = [(-weights[w], w) for w, _ in payouts]
        self.assertEqual(keys, sorted(keys))

    def test_tiny_pool_huge_holder_count_pays_only_top_weights(self):
        # Pool smaller than holder count: only holders whose floor share >= 1
        # get paid; the rest fall out. Sum still bounded by pool.
        weights = {f"h{i}": (i + 1) for i in range(1000)}  # weights 1..1000
        pool = 500
        payouts, leftover = dist.compute_payouts(pool, weights)
        self.assertLessEqual(sum(a for _, a in payouts), pool)
        self.assertEqual(sum(a for _, a in payouts) + leftover, pool)
        for _, amt in payouts:
            self.assertGreater(amt, 0)


class _DistributeIOBase(unittest.TestCase):
    """Shared mock harness for the I/O layer. Patches the rpc surface that
    `distribute` touches so no network is hit and confirmation is scripted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_payouts = config.PAYOUTS_PATH
        self._orig_data = config.DATA_DIR
        config.DATA_DIR = self.tmp
        config.PAYOUTS_PATH = os.path.join(self.tmp, "payouts.jsonl")
        # DRY_RUN must be off so the send path actually runs (against mocks).
        self._orig_dry = config.DRY_RUN
        config.DRY_RUN = False

    def tearDown(self):
        config.PAYOUTS_PATH = self._orig_payouts
        config.DATA_DIR = self._orig_data
        config.DRY_RUN = self._orig_dry

    def _payout_log_lines(self) -> list[str]:
        with open(config.PAYOUTS_PATH, "r", encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]


class TestDistributeIOAccounting(_DistributeIOBase):
    def _run(self, weights, *, confirm_fraction=1.0, n_decimals=6):
        from solders.pubkey import Pubkey

        token_program = config.TOKEN_PROGRAM
        pool = sum(weights.values()) * 1000  # plenty so everyone gets > 0
        sig_counter = count(1)
        sent_sigs: list[str] = []

        def fake_send(_b64, **_kw):
            s = f"sig{next(sig_counter)}"
            sent_sigs.append(s)
            return s

        def fake_statuses(batch):
            # Confirm the first `confirm_fraction` of all sigs ever sent;
            # mark the rest as on-chain failures (err set → excluded, no retry).
            out = []
            cutoff = int(len(sent_sigs) * confirm_fraction)
            confirmed_set = set(sent_sigs[:cutoff])
            for s in batch:
                if s in confirmed_set:
                    out.append({"err": None, "confirmationStatus": "confirmed"})
                else:
                    out.append({"err": {"InstructionError": [0, "x"]}, "confirmationStatus": "processed"})
            return out

        with mock.patch.object(dist.rpc, "get_spl_balance", return_value=pool), \
             mock.patch.object(dist.rpc, "get_recent_blockhash", return_value="11111111111111111111111111111111"), \
             mock.patch.object(dist.rpc, "send_raw_tx", side_effect=fake_send), \
             mock.patch.object(dist.rpc, "get_signature_statuses", side_effect=fake_statuses):
            result = dist.distribute(weights, token_program=token_program, decimals=n_decimals)
        return result, pool

    def test_all_confirmed_full_tally(self):
        weights = {w: i + 1 for i, w in enumerate(_wallets(20))}
        result, pool = self._run(weights, confirm_fraction=1.0)
        expected_payouts, leftover = dist.compute_payouts(pool, weights)
        self.assertEqual(result["recipients"], len(expected_payouts))
        self.assertEqual(result["sent"], len(expected_payouts))
        self.assertEqual(result["confirmed"], len(expected_payouts))
        self.assertEqual(result["actual_sent"], sum(a for _, a in expected_payouts))
        self.assertEqual(result["leftover_actual"], pool - result["actual_sent"])
        # One log line per attempted send.
        self.assertEqual(len(self._payout_log_lines()), len(expected_payouts))

    def test_partial_confirm_only_counts_confirmed(self):
        weights = {w: 10 for w in _wallets(40)}
        result, pool = self._run(weights, confirm_fraction=0.5)
        # Half confirmed → actual_sent must be strictly less than the full plan.
        full_plan = sum(a for _, a in dist.compute_payouts(pool, weights)[0])
        self.assertLess(result["actual_sent"], full_plan)
        self.assertEqual(result["confirmed"], result["sent"] // 2)
        # Unconfirmed tokens are treated as still in the wallet (conservative).
        self.assertEqual(result["leftover_actual"], pool - result["actual_sent"])

    def test_chunking_spans_multiple_blockhashes(self):
        # > DISTRIBUTE_CHUNK_SIZE recipients ⇒ more than one blockhash fetch.
        n = config.DISTRIBUTE_CHUNK_SIZE * 2 + 5
        weights = {w: 1 for w in _wallets(n)}
        with mock.patch.object(dist.rpc, "get_recent_blockhash", return_value="11111111111111111111111111111111") as bh:
            self._run_with_bh_spy(weights, bh)

    def _run_with_bh_spy(self, weights, bh_mock):
        # Re-run inside an already-patched blockhash mock to count chunk fetches.
        from solders.pubkey import Pubkey  # noqa: F401

        pool = sum(weights.values()) * 1000
        sent: list[str] = []
        c = count(1)

        def fake_send(_b64, **_kw):
            s = f"s{next(c)}"
            sent.append(s)
            return s

        def fake_statuses(batch):
            return [{"err": None, "confirmationStatus": "finalized"} for _ in batch]

        with mock.patch.object(dist.rpc, "get_spl_balance", return_value=pool), \
             mock.patch.object(dist.rpc, "send_raw_tx", side_effect=fake_send), \
             mock.patch.object(dist.rpc, "get_signature_statuses", side_effect=fake_statuses):
            result = dist.distribute(weights, token_program=config.TOKEN_PROGRAM, decimals=6)
        # 2 full chunks + 1 partial = 3 blockhash fetches.
        expected_chunks = (len(weights) + config.DISTRIBUTE_CHUNK_SIZE - 1) // config.DISTRIBUTE_CHUNK_SIZE
        self.assertEqual(bh_mock.call_count, expected_chunks)
        self.assertEqual(result["sent"], len(weights))
        self.assertEqual(result["confirmed"], len(weights))

    def test_dry_run_sends_nothing_but_plans(self):
        config.DRY_RUN = True
        try:
            weights = {w: 3 for w in _wallets(15)}
            with mock.patch.object(dist.rpc, "get_spl_balance", return_value=999_999), \
                 mock.patch.object(dist.rpc, "send_raw_tx", side_effect=AssertionError("must not send in DRY_RUN")), \
                 mock.patch.object(dist.rpc, "get_recent_blockhash", side_effect=AssertionError("no blockhash in DRY_RUN")):
                result = dist.distribute(weights, token_program=config.TOKEN_PROGRAM, decimals=6)
            self.assertTrue(result.get("dry_run"))
            self.assertEqual(result["sent"], 0)
            self.assertEqual(result["actual_sent"], 0)
        finally:
            config.DRY_RUN = False


class TestDistributeIOStress(_DistributeIOBase):
    def test_10k_recipients_send_and_confirm_tally(self):
        weights = {w: (i % 50) + 1 for i, w in enumerate(_wallets(10_000))}
        pool = sum(weights.values()) * 10
        c = count(1)
        sent: list[str] = []

        def fake_send(_b64, **_kw):
            s = f"x{next(c)}"
            sent.append(s)
            return s

        def fake_statuses(batch):
            return [{"err": None, "confirmationStatus": "confirmed"} for _ in batch]

        with mock.patch.object(dist.rpc, "get_spl_balance", return_value=pool), \
             mock.patch.object(dist.rpc, "get_recent_blockhash", return_value="11111111111111111111111111111111"), \
             mock.patch.object(dist.rpc, "send_raw_tx", side_effect=fake_send), \
             mock.patch.object(dist.rpc, "get_signature_statuses", side_effect=fake_statuses):
            result = dist.distribute(weights, token_program=config.TOKEN_PROGRAM, decimals=6)

        expected_payouts, _ = dist.compute_payouts(pool, weights)
        self.assertEqual(result["sent"], len(expected_payouts))
        self.assertEqual(result["confirmed"], len(expected_payouts))
        self.assertEqual(result["actual_sent"], sum(a for _, a in expected_payouts))
        self.assertLessEqual(result["actual_sent"], pool)
        self.assertEqual(len(self._payout_log_lines()), len(expected_payouts))


if __name__ == "__main__":
    unittest.main()
