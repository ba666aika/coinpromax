"""Large-volume stress tests for the held-seconds tracker.

The user launches a stream and cannot fix things live, so the tracker must stay
correct and fast at thousands of holders churning every cycle. These prove:
  - accrual is correct across 10k holders over many ticks
  - a mass partial-sell event resets exactly the sellers and nobody else
  - dropping below MIN_HOLDING_RAW exits a holder
  - save/load round-trips a 10k-entry state without corruption
"""
from __future__ import annotations

import os
import tempfile
import time
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
from bot import loyalty_tracker as lt  # noqa: E402

N = 10_000


class TestTrackerStress(unittest.TestCase):
    def setUp(self):
        self.owners = [f"holder_{i}" for i in range(N)]
        self.t0 = 1_000_000

    def test_accrual_then_mass_partial_sell(self):
        state: dict[str, dict] = {}
        balances = {o: 100_000 for o in self.owners}

        # Tick 1: everyone enters.
        t = time.perf_counter()
        lt.update(state, balances, now=self.t0)
        elapsed_seed = time.perf_counter() - t
        self.assertEqual(len(state), N)
        self.assertTrue(all(state[o]["held_seconds"] == 0 for o in self.owners))

        # Tick 2: +60s, all hold.
        lt.update(state, balances, now=self.t0 + 60)
        self.assertTrue(all(state[o]["held_seconds"] == 60 for o in self.owners))

        # Tick 3: +60s. Even-indexed holders sell down (partial), odd hold.
        sellers = set(self.owners[::2])
        next_balances = {o: (50_000 if o in sellers else 100_000) for o in self.owners}
        t = time.perf_counter()
        lt.update(state, next_balances, now=self.t0 + 120)
        elapsed_update = time.perf_counter() - t

        for o in self.owners:
            if o in sellers:
                self.assertEqual(state[o]["held_seconds"], 0, f"{o} should reset")
                self.assertEqual(state[o]["first_seen_ts"], self.t0 + 120)
                self.assertEqual(state[o]["last_balance"], 50_000)
            else:
                self.assertEqual(state[o]["held_seconds"], 120, f"{o} should accrue")

        # Eligibility: only the holders (odd) accrued > 0; sellers reset to 0.
        weights = lt.eligible_weights(state)
        self.assertEqual(len(weights), N - len(sellers))
        self.assertTrue(all(o not in weights for o in sellers))
        self.assertTrue(all(weights[o] == 120 for o in self.owners[1::2]))

        # Performance guardrail — 10k holders must process in well under a second.
        self.assertLess(elapsed_seed, 1.0)
        self.assertLess(elapsed_update, 1.0)

    def test_below_min_holding_exits_holder(self):
        with mock.patch.object(config, "MIN_HOLDING_RAW", 50_000):
            state: dict[str, dict] = {}
            balances = {o: 60_000 for o in self.owners}
            lt.update(state, balances, now=self.t0)
            lt.update(state, balances, now=self.t0 + 100)  # accrue 100
            self.assertTrue(all(state[o]["held_seconds"] == 100 for o in self.owners))

            # Half drop below the 50k minimum (but stay > 0).
            droppers = set(self.owners[::2])
            nb = {o: (40_000 if o in droppers else 60_000) for o in self.owners}
            lt.update(state, nb, now=self.t0 + 200)

            weights = lt.eligible_weights(state)
            self.assertTrue(all(o not in weights for o in droppers))
            self.assertEqual(len(weights), N - len(droppers))

    def test_long_run_endurance_with_churn(self):
        """Simulate the tracker over MANY cycles with churn — diamonds never sell,
        half the churners dump on a periodic cadence — and prove the numbers stay
        exact and per-tick cost stays bounded. ~10h simulated (300 cycles * 120s)."""
        with mock.patch.object(config, "MIN_HOLDING_RAW", 50_000):
            P = 120
            CYCLES = 300
            HOLD = 80_000
            diamonds = [f"diamond_{i}" for i in range(2_000)]   # never sell
            churn = [f"churn_{i}" for i in range(2_000)]        # dump periodically
            steady = diamonds + churn[1::2]                     # churn odds never sell
            dumpers = churn[::2]                                # churn evens dump
            state: dict[str, dict] = {}
            worst_tick = 0.0
            last_dump_cycle = 0
            for c in range(CYCLES):
                t = self.t0 + c * P
                bals = {w: HOLD for w in steady}
                dumping = c > 0 and c % 50 == 0
                if dumping:
                    last_dump_cycle = c
                for w in dumpers:
                    bals[w] = 0 if dumping else HOLD
                t_perf = time.perf_counter()
                lt.update(state, bals, now=t)
                worst_tick = max(worst_tick, time.perf_counter() - t_perf)
            for w in steady:
                self.assertEqual(state[w]["held_seconds"], (CYCLES - 1) * P, w)
            entry = last_dump_cycle + 1
            expected_dumper = (CYCLES - 1 - entry) * P
            for w in dumpers:
                self.assertEqual(state[w]["held_seconds"], expected_dumper, w)
            weights = lt.eligible_weights(state)
            self.assertEqual(len(weights), len(diamonds) + len(churn))
            self.assertTrue(all(v > 0 for v in weights.values()))
            self.assertTrue(all(state[w]["last_balance"] >= 50_000 for w in weights))
            self.assertEqual(len(state), len(diamonds) + len(churn))
            self.assertLess(worst_tick, 0.5)
            tmp = tempfile.mkdtemp()
            path = os.path.join(tmp, "state.json")
            with mock.patch.object(config, "LOYALTY_STATE_PATH", path):
                lt.save_state(state)
                reloaded = lt.load_state()
            self.assertEqual(reloaded, state)

    def test_tasks_at_scale(self):
        """Task flags add an O(1) per-wallet check; prove apply_task +
        task_weights are correct + fast at 10k holders, and that the sticky
        flags survive an empty refresh."""
        state: dict[str, dict] = {}
        bals = {o: 100_000 for o in self.owners}
        lt.update(state, bals, now=self.t0)
        lt.update(state, bals, now=self.t0 + 100)         # all hold + accrued
        self.assertEqual(len(lt.task_weights(state, "callout")), 0)  # nobody yet
        callout = set(self.owners[::2])                   # half comment the thread
        t = time.perf_counter()
        lt.apply_task(state, callout, "callout", now=self.t0 + 100)
        weights = lt.task_weights(state, "callout")
        elapsed = time.perf_counter() - t
        self.assertEqual(len(weights), len(callout))
        self.assertTrue(all(o in callout for o in weights))
        self.assertLess(elapsed, 0.5)                     # apply+filter over 10k well under 0.5s
        lt.apply_task(state, set(), "callout")            # empty/failed refresh
        self.assertEqual(len(lt.task_weights(state, "callout")), len(callout))  # sticky
        # the other task is independent
        self.assertEqual(len(lt.task_weights(state, "bullpost")), 0)

    def test_save_load_round_trip_at_scale(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "state.json")
        with mock.patch.object(config, "LOYALTY_STATE_PATH", path):
            state: dict[str, dict] = {}
            lt.update(state, {o: 12_345 for o in self.owners}, now=self.t0)
            lt.update(state, {o: 12_345 for o in self.owners}, now=self.t0 + 30)
            lt.save_state(state)
            reloaded = lt.load_state()
        self.assertEqual(len(reloaded), N)
        self.assertEqual(reloaded, state)


if __name__ == "__main__":
    unittest.main()
