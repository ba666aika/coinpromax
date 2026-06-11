"""Regression tests for the core loyalty mechanic.

These tests encode the non-negotiable behaviors of the coin. If any of
them fails after a refactor, the refactor is wrong — not the test.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock


# Provide dummy env so `import bot.config` doesn't sys.exit during test collection.
# Generate a real throwaway keypair so the base58 validation in config.py passes.
from solders.keypair import Keypair as _TestKp  # noqa: E402

_test_kp = _TestKp()
_test_pubkey = str(_test_kp.pubkey())

os.environ.setdefault("HELIUS_API_KEY", "test")
os.environ.setdefault("WALLET_PRIVATE_KEY", str(_test_kp))
os.environ.setdefault("LOYALTY_MINT", "2jCt3hj9vd7YpV7Sr3VA5nk3tdSpJtZezeoJXW4Xpump")
# A second throwaway keypair for OPERATOR_WALLET.
os.environ.setdefault("OPERATOR_WALLET", str(_TestKp().pubkey()))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("MIN_HOLDING_RAW", "1")

from bot import loyalty_tracker as lt  # noqa: E402


class TestHeldSecondsAccrual(unittest.TestCase):
    def test_new_wallet_seeded_with_zero_held(self):
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        self.assertEqual(state["alice"]["held_seconds"], 0)
        self.assertEqual(state["alice"]["first_seen_ts"], 1000)
        self.assertEqual(state["alice"]["last_balance"], 100)

    def test_accrues_seconds_when_balance_unchanged(self):
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        lt.update(state, {"alice": 100}, now=1060)
        self.assertEqual(state["alice"]["held_seconds"], 60)

    def test_accrues_seconds_when_balance_increased_buyadd(self):
        """Buy-add does NOT reset — time keeps accruing."""
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        lt.update(state, {"alice": 250}, now=1060)
        self.assertEqual(state["alice"]["held_seconds"], 60)
        self.assertEqual(state["alice"]["last_balance"], 250)


class TestSellResets(unittest.TestCase):
    def test_any_balance_decrease_resets_held_seconds_to_zero(self):
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        lt.update(state, {"alice": 100}, now=2000)  # accrues 1000s
        self.assertEqual(state["alice"]["held_seconds"], 1000)
        # Even tiny sell triggers reset.
        lt.update(state, {"alice": 99}, now=2001)
        self.assertEqual(state["alice"]["held_seconds"], 0)
        self.assertEqual(state["alice"]["last_balance"], 99)
        self.assertEqual(state["alice"]["first_seen_ts"], 2001)

    def test_wallet_missing_from_snapshot_is_treated_as_zero_and_resets(self):
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        lt.update(state, {}, now=2000)
        self.assertEqual(state["alice"]["held_seconds"], 0)
        self.assertEqual(state["alice"]["last_balance"], 0)

    def test_re_entry_after_reset_starts_fresh(self):
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        lt.update(state, {"alice": 100}, now=2000)
        lt.update(state, {"alice": 50}, now=2001)  # sell → reset
        self.assertEqual(state["alice"]["held_seconds"], 0)
        # Then buy back in.
        lt.update(state, {"alice": 200}, now=3001)
        self.assertEqual(state["alice"]["held_seconds"], 1000)  # 3001 - 2001
        self.assertEqual(state["alice"]["first_seen_ts"], 2001)


class TestFloorReEntry(unittest.TestCase):
    """Crossing the 50k eligibility floor is a fresh entry, not a continuation.

    Distinct from TestSellResets.test_re_entry_after_reset_starts_fresh: there the
    wallet sells but stays ABOVE the floor, so it keeps being eligible and accrues
    across the (continuously-held) gap. Here the wallet drops BELOW the floor — it
    was ineligible during the gap — so re-crossing must NOT credit that time.
    """

    def test_recrossing_floor_from_below_starts_at_zero(self):
        with mock.patch.object(lt.config, "MIN_HOLDING_RAW", 50_000):
            state: dict[str, dict] = {}
            lt.update(state, {"alice": 60_000}, now=1000)
            lt.update(state, {"alice": 60_000}, now=2000)          # accrue 1000s
            self.assertEqual(state["alice"]["held_seconds"], 1000)

            # Drop BELOW the floor → reset + ineligible.
            lt.update(state, {"alice": 40_000}, now=2100)
            self.assertEqual(state["alice"]["held_seconds"], 0)
            self.assertNotIn("alice", lt.eligible_weights(state))

            # Sit below the floor for a long stretch (the would-be free window).
            lt.update(state, {"alice": 40_000}, now=9000)
            self.assertEqual(state["alice"]["held_seconds"], 0)

            # Re-cross the floor: starts at zero, NOT credited for the 7900s gap.
            lt.update(state, {"alice": 80_000}, now=10000)
            self.assertEqual(state["alice"]["held_seconds"], 0)
            self.assertEqual(state["alice"]["first_seen_ts"], 10000)
            self.assertNotIn("alice", lt.eligible_weights(state))   # held=0 this tick

            # Next tick above the floor → accrues like any new holder.
            lt.update(state, {"alice": 80_000}, now=10060)
            self.assertEqual(state["alice"]["held_seconds"], 60)

    def test_sell_but_stay_above_floor_accrues_from_reset(self):
        """Contrast case (unchanged behavior): a decrease that stays ABOVE the
        floor resets, then accrues from the reset across the held gap."""
        with mock.patch.object(lt.config, "MIN_HOLDING_RAW", 50_000):
            state: dict[str, dict] = {}
            lt.update(state, {"alice": 100_000}, now=1000)
            lt.update(state, {"alice": 100_000}, now=2000)
            lt.update(state, {"alice": 70_000}, now=2001)          # sell, still >= 50k
            self.assertEqual(state["alice"]["held_seconds"], 0)
            lt.update(state, {"alice": 90_000}, now=3001)          # buy-add, no decrease
            self.assertEqual(state["alice"]["held_seconds"], 1000)


class TestEligibility(unittest.TestCase):
    def test_held_seconds_zero_means_not_eligible_this_cycle(self):
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        # held_seconds is 0 right after seeding.
        weights = lt.eligible_weights(state)
        self.assertNotIn("alice", weights)

    def test_eligible_after_one_tick(self):
        state = {}
        lt.update(state, {"alice": 100}, now=1000)
        lt.update(state, {"alice": 100}, now=1001)
        weights = lt.eligible_weights(state)
        self.assertEqual(weights["alice"], 1)

    def test_excluded_owners_filtered(self):
        state = {}
        lt.update(state, {"alice": 100, "amm_pool": 9999}, now=1000)
        lt.update(state, {"alice": 100, "amm_pool": 9999}, now=2000)
        weights = lt.eligible_weights(state)
        filtered = lt.filter_excluded(weights, {"amm_pool"})
        self.assertIn("alice", filtered)
        self.assertNotIn("amm_pool", filtered)


class TestStorageAtomicity(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        state = {"alice": {"first_seen_ts": 1000, "last_balance": 100, "last_check_ts": 1000, "held_seconds": 0}}
        lt.save_state(state)
        reloaded = lt.load_state()
        self.assertEqual(state, reloaded)

    def test_missing_file_returns_empty(self):
        # Move state path to a fresh location.
        import bot.config as cfg
        with mock.patch.object(cfg, "LOYALTY_STATE_PATH", "/tmp/nonexistent_loyalty_state_xyz.json"):
            with mock.patch.object(lt.config, "LOYALTY_STATE_PATH", "/tmp/nonexistent_loyalty_state_xyz.json"):
                if os.path.exists(lt.config.LOYALTY_STATE_PATH):
                    os.remove(lt.config.LOYALTY_STATE_PATH)
                self.assertEqual(lt.load_state(), {})


class TestTasks(unittest.TestCase):
    """CPM tasks: sticky flags ('callout' / 'bullpost'), per-task weight sets,
    and the level computed from hold + tasks."""

    def _held(self, ts0=1000):
        state = {}
        lt.update(state, {"alice": 100, "bob": 100}, now=ts0)
        lt.update(state, {"alice": 100, "bob": 100}, now=ts0 + 100)  # both accrue, eligible
        return state

    def test_apply_task_is_sticky_and_safe(self):
        state = self._held()
        lt.apply_task(state, {"alice"}, "callout", now=2000)
        self.assertEqual(state["alice"]["tasks"], {"callout": 2000})
        # a later empty/failed refresh must NOT revoke it
        lt.apply_task(state, set(), "callout")
        self.assertIn("callout", state["alice"]["tasks"])
        # re-applying never overwrites the first-done timestamp
        lt.apply_task(state, {"alice"}, "callout", now=9999)
        self.assertEqual(state["alice"]["tasks"]["callout"], 2000)
        # unknown wallets (did the task before holding) are ignored, no crash
        lt.apply_task(state, {"ghost"}, "callout")
        self.assertNotIn("ghost", state)

    def test_task_weights_filters_by_task(self):
        state = self._held()
        lt.apply_task(state, {"alice"}, "callout")
        lt.apply_task(state, {"bob"}, "bullpost")
        self.assertEqual(set(lt.task_weights(state, "callout")), {"alice"})
        self.assertEqual(set(lt.task_weights(state, "bullpost")), {"bob"})
        # weights match the global weighted_holdings values
        w = lt.weighted_holdings(state)
        self.assertEqual(lt.task_weights(state, "callout")["alice"], w["alice"])

    def test_tasks_survive_save_load(self):
        state = self._held()
        lt.apply_task(state, {"alice"}, "bullpost", now=1500)
        lt.save_state(state)
        reloaded = lt.load_state()
        self.assertEqual(reloaded["alice"]["tasks"], {"bullpost": 1500})

    def test_tasks_survive_sell_reset_but_weight_zeroes(self):
        """Selling resets the accumulated TIME (weight → 0) but keeps the sticky
        task flags — the call-out still exists on pump.fun; the punishment is
        rebuilding held time from zero."""
        state = self._held()
        lt.apply_task(state, {"alice"}, "callout")
        lt.update(state, {"alice": 50, "bob": 100}, now=2000)  # alice sells
        self.assertIn("callout", state["alice"]["tasks"])       # flag survives
        self.assertEqual(state["alice"]["held_seconds"], 0)     # time reset
        self.assertNotIn("alice", lt.task_weights(state, "callout"))  # weight 0 → unpaid

    def test_level_of(self):
        state = self._held()
        self.assertEqual(lt.level_of(state["alice"]), 1)        # holding, no tasks
        lt.apply_task(state, {"alice"}, "callout")
        self.assertEqual(lt.level_of(state["alice"]), 2)
        lt.apply_task(state, {"alice"}, "bullpost")
        self.assertEqual(lt.level_of(state["alice"]), 3)
        # sell → reset → level 0 until time re-accrues (tasks kept)
        lt.update(state, {"alice": 50, "bob": 100}, now=3000)
        self.assertEqual(lt.level_of(state["alice"]), 0)
        lt.update(state, {"alice": 50, "bob": 100}, now=3060)   # re-accrues
        self.assertEqual(lt.level_of(state["alice"]), 3)


class TestCpmMechanics(unittest.TestCase):
    """CPM: any sell -> full reset of accumulated time, NO permanent flag;
    payout weight = held_seconds x balance."""

    def test_sell_resets_without_sticky_flag(self):
        state: dict[str, dict] = {}
        lt.update(state, {"a": 100}, now=1000)
        lt.update(state, {"a": 100}, now=2000)
        lt.update(state, {"a": 50}, now=2001)          # decrease = sold → reset
        self.assertEqual(state["a"]["held_seconds"], 0)
        self.assertNotIn("sold", state["a"])           # no USUG gay-flag in CPM
        # restart is clean: still above the floor, so time re-accrues from the
        # reset point (2001) exactly like the documented engine behavior
        lt.update(state, {"a": 999}, now=3001)
        self.assertEqual(state["a"]["held_seconds"], 1000)
        self.assertEqual(state["a"]["first_seen_ts"], 2001)

    def test_weighted_holdings_is_time_times_amount(self):
        state: dict[str, dict] = {}
        lt.update(state, {"a": 100, "b": 300}, now=1000)
        lt.update(state, {"a": 100, "b": 300}, now=1010)  # held=10 each
        w = lt.weighted_holdings(state)
        self.assertEqual(w["a"], 10 * 100)
        self.assertEqual(w["b"], 10 * 300)
        self.assertEqual(w["b"], 3 * w["a"])           # 3x amount, same time -> 3x weight


if __name__ == "__main__":
    unittest.main()
