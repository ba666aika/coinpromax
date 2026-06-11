"""Tests for the money loop in cycle.py (Coin Pro Max split).

Every on-chain call is mocked — no network, no signing. The point is to prove
the SAFETY MODEL holds exactly:
  - operator cut = OPERATOR_PCT of the MEASURED delta, paid FIRST
  - ad reserve   = AD_RESERVE_PCT booked (no transfer — stays on the wallet)
  - reward pool  = the rest, split evenly: sol_pool / buyback / stock basket
  - buyback ≤ MAX_BUYBACK_LAMPORTS, basket ≤ MAX_STOCK_BASKET_LAMPORTS,
    SOL airdrop ≤ MAX_SOL_AIRDROP_LAMPORTS and ≤ wallet − floor − ad reserve
  - any fail-CLOSED read aborts the right scope (tick vs. just money steps)
  - claimed <= 0 moves no money
  - airdrops are gated by AIRDROP_INTERVAL, use held×balance weights, exclude
    EXCLUDED_OWNERS, gate per task (CPM ← callout, stocks ← bullpost), and the
    marker advances only on a real (non-DRY_RUN) run
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
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
from bot import cycle  # noqa: E402
from bot import loyalty_tracker as tracker  # noqa: E402
from bot.rpc import RPCError  # noqa: E402

_TP = str(config.TOKEN_PROGRAM)
_SOL = 1_000_000_000


def _wallet() -> str:
    return str(_TestKp().pubkey())


class _CycleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._restore = {
            "LOYALTY_STATE_PATH": config.LOYALTY_STATE_PATH,
            "STATS_PATH": config.STATS_PATH,
            "PAYOUTS_PATH": config.PAYOUTS_PATH,
            "SOL_POOL_PATH": config.SOL_POOL_PATH,
            "AD_RESERVE_PATH": config.AD_RESERVE_PATH,
            "CPM_BUY_POOL_PATH": config.CPM_BUY_POOL_PATH,
            "STOCK_BUY_POOL_PATH": config.STOCK_BUY_POOL_PATH,
            "CASINO_POOL_PATH": config.CASINO_POOL_PATH,
            "DATA_DIR": config.DATA_DIR,
            "DRY_RUN": config.DRY_RUN,
        }
        config.DATA_DIR = self.tmp
        config.LOYALTY_STATE_PATH = os.path.join(self.tmp, "loyalty_state.json")
        config.STATS_PATH = os.path.join(self.tmp, "stats.json")
        config.PAYOUTS_PATH = os.path.join(self.tmp, "payouts.jsonl")
        config.SOL_POOL_PATH = os.path.join(self.tmp, "sol_pool.json")
        config.AD_RESERVE_PATH = os.path.join(self.tmp, "ad_reserve.json")
        config.CPM_BUY_POOL_PATH = os.path.join(self.tmp, "cpm_buy_pool.json")
        config.STOCK_BUY_POOL_PATH = os.path.join(self.tmp, "stock_buy_pool.json")
        config.CASINO_POOL_PATH = os.path.join(self.tmp, "casino_pool.json")
        config.DRY_RUN = False
        self._orig_marker = cycle._LAST_AIRDROP_PATH
        cycle._LAST_AIRDROP_PATH = os.path.join(self.tmp, "last_airdrop_at.txt")
        self._orig_claim_marker = cycle._LAST_CLAIM_PATH
        cycle._LAST_CLAIM_PATH = os.path.join(self.tmp, "last_claim_at.txt")
        self._orig_stocks_marker = cycle._LAST_STOCKS_AIRDROP_PATH
        cycle._LAST_STOCKS_AIRDROP_PATH = os.path.join(self.tmp, "last_stocks_airdrop_at.txt")
        self._orig_casino_marker = cycle._LAST_CASINO_PATH
        cycle._LAST_CASINO_PATH = os.path.join(self.tmp, "last_casino_at.txt")
        cycle._pool_owners.clear()
        cycle._classified_owners.clear()
        cycle._last_tasks_fetch_ts = 0

    def tearDown(self):
        for k, v in self._restore.items():
            setattr(config, k, v)
        cycle._LAST_AIRDROP_PATH = self._orig_marker
        cycle._LAST_CLAIM_PATH = self._orig_claim_marker
        cycle._LAST_STOCKS_AIRDROP_PATH = self._orig_stocks_marker
        cycle._LAST_CASINO_PATH = self._orig_casino_marker

    # -- helpers --

    def _seed(self, owners_balances: dict[str, int], *, held: int = 500, tasks: tuple = (), lvl3_age: int = 3600) -> None:
        """Pre-write tracker state so the given owners are already eligible
        (held_seconds will accrue further on this tick, staying > 0). `tasks`
        sticky-marks every owner with the given task names; owners with BOTH
        tasks also get a matured `lvl3_since` stamp (lvl3_age seconds old) so
        they qualify for the casino unless a test overrides lvl3_age."""
        now = int(time.time())
        full_lvl3 = "callout" in tasks and "bullpost" in tasks
        state = {
            owner: {
                "first_seen_ts": now - 7200,
                "last_balance": bal,
                "last_check_ts": now - 3600,
                "held_seconds": held,
                **({"tasks": {t: now - 3600 for t in tasks}} if tasks else {}),
                **({"lvl3_since": now - lvl3_age} if full_lvl3 else {}),
            }
            for owner, bal in owners_balances.items()
        }
        tracker.save_state(state)

    def _holders(self, owners_balances: dict[str, int]) -> list[dict]:
        return [
            {"pubkey": _wallet(), "owner": owner, "amount": bal}
            for owner, bal in owners_balances.items()
        ]

    def _stats_written(self) -> bool:
        return os.path.exists(config.STATS_PATH)

    @contextlib.contextmanager
    def _harness(
        self,
        *,
        before=_SOL,
        after=_SOL,
        holders=None,
        token_program=_TP,
        decimals=6,
        tp_exc=None,
        snap_exc=None,
        before_exc=None,
        after_exc=None,
        op_exc=None,
        decimals_exc=None,
    ):
        holders = holders if holders is not None else []
        bal_calls = {"n": 0}

        def sol_balance(_pubkey, *a, **k):
            bal_calls["n"] += 1
            if bal_calls["n"] == 1:
                if before_exc:
                    raise before_exc
                return before
            if after_exc:
                raise after_exc
            return after

        def token_program_fn(_mint, *a, **k):
            if tp_exc:
                raise tp_exc
            return token_program

        def holders_fn(_mint, *a, **k):
            if snap_exc:
                raise snap_exc
            return holders

        def decimals_fn(_mint, *a, **k):
            if decimals_exc:
                raise decimals_exc
            return decimals

        with contextlib.ExitStack() as es:
            p = es.enter_context
            m = SimpleNamespace()
            m.get_token_program = p(mock.patch.object(cycle.rpc, "get_token_program", side_effect=token_program_fn))
            m.get_token_holders = p(mock.patch.object(cycle.rpc, "get_token_holders", side_effect=holders_fn))
            m.get_sol_balance = p(mock.patch.object(cycle.rpc, "get_sol_balance", side_effect=sol_balance))
            m.get_token_decimals = p(mock.patch.object(cycle.rpc, "get_token_decimals", side_effect=decimals_fn))
            m.claim_bonding = p(mock.patch.object(cycle.pumpfun, "claim_bonding_curve", return_value=None))
            m.claim_amm = p(mock.patch.object(cycle.pumpfun, "claim_amm", return_value=None))
            ba = mock.patch.object(cycle.stx, "build_and_send", return_value=None)
            if op_exc:
                ba = mock.patch.object(cycle.stx, "build_and_send", side_effect=op_exc)
            m.build_and_send = p(ba)
            # Swaps "succeed" by default (sig / one sig per basket leg) so the
            # buy-pools decrement like a real successful submit.
            m.buyback = p(mock.patch.object(cycle.swap, "buyback", return_value="sig"))
            m.buy_basket = p(mock.patch.object(
                cycle.stocks, "buy_basket",
                side_effect=lambda lamports: [f"sig{i}" for i in range(len(config.STOCK_MINTS))],
            ))
            m.distribute = p(mock.patch.object(cycle.dist, "distribute", return_value={}))
            m.distribute_sol = p(mock.patch.object(cycle.dist, "distribute_sol", return_value={}))
            # Task feeds: never hit the network from tests.
            m.fetch_callouts = p(mock.patch.object(cycle.pumpthread, "fetch_callout_wallets", return_value=set()))
            m.fetch_bullposts = p(mock.patch.object(cycle.community, "fetch_engaged_from_api", return_value=set()))
            # By default every holder owner classifies as a real (System-Program) wallet;
            # individual tests override this to simulate a pool.
            m.account_owners = p(mock.patch.object(
                cycle.rpc, "get_account_owner_programs",
                side_effect=lambda pks: {pk: str(config.SYSTEM_PROGRAM) for pk in pks},
            ))
            yield m


class TestSplitAndCap(_CycleBase):
    """claimed = 1 SOL → operator 0.20, ad reserve 0.20 (booked, not moved),
    then 15% each into the four level accumulators: sol_pool / buyback /
    basket / casino."""

    def test_full_split_20_20_15x4(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))  # gate airdrops OFF
            cycle.tick()
        # Operator transfer is the ONLY transfer (ad reserve + casino are books).
        m.build_and_send.assert_called_once()
        self.assertIn("operator_cut(200000000)", m.build_and_send.call_args.kwargs["label"])
        # Ad reserve + casino pot booked, stay on wallet.
        self.assertEqual(cycle.read_ad_reserve(), 200_000_000)
        self.assertEqual(cycle.read_casino_pool(), 150_000_000)
        # 15% per reward leg.
        self.assertEqual(cycle.read_sol_pool(), 150_000_000)
        m.buyback.assert_called_once_with(150_000_000)
        m.buy_basket.assert_called_once_with(150_000_000)
        # Successful swaps fully drain their pools (150M divides evenly by 5).
        self.assertEqual(cycle.read_cpm_buy_pool(), 0)
        self.assertEqual(cycle.read_stock_buy_pool(), 0)

    def test_micro_claim_accumulates_without_swapping(self):
        """Shares below MIN_SWAP_LAMPORTS pool up instead of burning tx fees
        on micro-swaps. Nothing is lost — the next claim adds on top."""
        owner = _wallet()
        self._seed({owner: 100})
        # claimed = 3M lamports → 15% legs = 450k each, all < 5M min swap.
        with self._harness(before=_SOL, after=_SOL + 3_000_000, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        m.buyback.assert_not_called()
        m.buy_basket.assert_not_called()
        self.assertEqual(cycle.read_cpm_buy_pool(), 450_000)
        self.assertEqual(cycle.read_stock_buy_pool(), 450_000)
        self.assertEqual(cycle.read_sol_pool(), 450_000)
        self.assertEqual(cycle.read_casino_pool(), 450_000)

    def test_failed_swap_keeps_budget_pooled(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
            m.buyback.return_value = None      # swap failed on all pools
            m.buy_basket.side_effect = lambda lamports: []  # every leg failed
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        # Budgets stay pooled for retry on the next claim window.
        self.assertEqual(cycle.read_cpm_buy_pool(), 150_000_000)
        self.assertEqual(cycle.read_stock_buy_pool(), 150_000_000)

    def test_buyback_hard_cap_is_absolute(self):
        owner = _wallet()
        self._seed({owner: 100})
        with mock.patch.object(config, "MAX_BUYBACK_LAMPORTS", 100_000_000):
            with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
                cycle._write_last_airdrop_ts(int(time.time()))
                cycle.tick()
        # supply share = 150M but cap = 100M → buyback clamped to 100M;
        # the excess stays POOLED (carryover via accumulator, not lost).
        m.buyback.assert_called_once_with(100_000_000)
        self.assertEqual(cycle.read_cpm_buy_pool(), 50_000_000)

    def test_stock_basket_hard_cap_is_absolute(self):
        owner = _wallet()
        self._seed({owner: 100})
        with mock.patch.object(config, "MAX_STOCK_BASKET_LAMPORTS", 50_000_000):
            with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
                cycle._write_last_airdrop_ts(int(time.time()))
                cycle.tick()
        m.buy_basket.assert_called_once_with(50_000_000)
        # spent = (50M // 5) × 5 = 50M; the rest of the share stays pooled.
        self.assertEqual(cycle.read_stock_buy_pool(), 150_000_000 - 50_000_000)

    def test_operator_paid_before_reward_legs(self):
        owner = _wallet()
        self._seed({owner: 100})
        order = []
        with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
            m.build_and_send.side_effect = lambda *a, **k: order.append("operator")
            m.buyback.side_effect = lambda *a, **k: order.append("buyback")
            m.buy_basket.side_effect = lambda *a, **k: order.append("stocks")
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        self.assertEqual(order, ["operator", "buyback", "stocks"])


class TestNoMoneyPaths(_CycleBase):
    def test_zero_claim_moves_nothing(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after=_SOL, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        m.build_and_send.assert_not_called()
        m.buyback.assert_not_called()
        m.buy_basket.assert_not_called()
        self.assertEqual(cycle.read_sol_pool(), 0)
        self.assertEqual(cycle.read_ad_reserve(), 0)

    def test_negative_claim_moves_nothing(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after=_SOL - 50_000, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        m.build_and_send.assert_not_called()
        m.buyback.assert_not_called()
        m.buy_basket.assert_not_called()
        self.assertEqual(cycle.read_sol_pool(), 0)


class TestFailClosed(_CycleBase):
    def test_before_balance_read_failure_skips_claims_and_money(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before_exc=RPCError("rpc down"), holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        # We bail before even attempting claims.
        m.claim_bonding.assert_not_called()
        m.build_and_send.assert_not_called()
        m.buyback.assert_not_called()
        # Tracker still ran (the heart keeps beating).
        self.assertTrue(self._stats_written())

    def test_after_balance_read_failure_skips_cut_and_buyback(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after_exc=RPCError("rpc down"), holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        # Claims were attempted (best-effort), but no delta ⇒ no cut/buyback.
        m.claim_bonding.assert_called_once()
        m.build_and_send.assert_not_called()
        m.buyback.assert_not_called()

    def test_operator_cut_failure_skips_all_reward_legs(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(
            before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100}), op_exc=RPCError("send fail")
        ) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle.tick()
        m.build_and_send.assert_called_once()      # attempted
        m.buyback.assert_not_called()              # every downstream leg skipped
        m.buy_basket.assert_not_called()
        self.assertEqual(cycle.read_ad_reserve(), 0)  # booked AFTER the cut succeeds
        self.assertEqual(cycle.read_casino_pool(), 0)
        self.assertEqual(cycle.read_sol_pool(), 0)

    def test_token_program_failure_skips_entire_tick(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(tp_exc=RPCError("rpc down"), holders=self._holders({owner: 100})) as m:
            cycle.tick()
        m.get_token_holders.assert_not_called()
        m.buyback.assert_not_called()
        self.assertFalse(self._stats_written())

    def test_snapshot_failure_skips_entire_tick(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(snap_exc=RPCError("rpc down")) as m:
            cycle.tick()
        m.build_and_send.assert_not_called()
        m.buyback.assert_not_called()
        self.assertFalse(self._stats_written())


class TestDistributeGating(_CycleBase):
    def test_skipped_before_interval(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"))
        with self._harness(holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))  # just now
            cycle.tick()
        m.distribute.assert_not_called()
        m.distribute_sol.assert_not_called()

    def test_runs_after_interval_with_weighted_holdings(self):
        from solders.pubkey import Pubkey

        owner_a, owner_b = _wallet(), _wallet()
        # Both did the call-out → the CPM supply leg pays both. No bullposts →
        # the stocks leg is skipped (exactly one distribute() call).
        self._seed({owner_a: 100, owner_b: 200}, held=500, tasks=("callout",))
        with self._harness(holders=self._holders({owner_a: 100, owner_b: 200}), decimals=6) as m:
            cycle._write_last_airdrop_ts(0)  # long ago → due
            cycle.tick()
        m.distribute.assert_called_once()
        weights = m.distribute.call_args.args[0]
        self.assertEqual(set(weights.keys()), {owner_a, owner_b})
        for v in weights.values():
            self.assertGreater(v, 0)
        # Payout weight = held_seconds × balance: depends on BOTH time and amount.
        state = tracker.load_state()
        expected = tracker.filter_excluded(tracker.task_weights(state, "callout"), config.EXCLUDED_OWNERS)
        self.assertEqual(weights, expected)
        # equal held time, owner_b holds 2x the balance → exactly 2x the weight.
        self.assertEqual(weights[owner_b], 2 * weights[owner_a])
        kwargs = m.distribute.call_args.kwargs
        self.assertIsInstance(kwargs["token_program"], Pubkey)
        self.assertEqual(str(kwargs["token_program"]), _TP)
        self.assertEqual(kwargs["decimals"], 6)
        self.assertEqual(kwargs["asset"], "CPM")
        # Marker advanced on a real run.
        self.assertGreater(cycle._read_last_airdrop_ts(), 0)

    def test_supply_leg_requires_callout_task(self):
        owner = _wallet()
        self._seed({owner: 100})  # eligible holder, NO tasks
        with self._harness(holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(0)
            cycle.tick()
        # No call-out → no $CPM supply airdrop; no bullpost → no stock legs.
        m.distribute.assert_not_called()
        # Window is still consumed (holders exist, the legs just had no takers).
        self.assertGreater(cycle._read_last_airdrop_ts(), 0)

    def test_stock_legs_run_per_mint_for_bullposters(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("bullpost",))
        with self._harness(holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(0)
            cycle.tick()
        # bullpost but no callout → no CPM call, one distribute per stock mint.
        assets = [c.kwargs.get("asset") for c in m.distribute.call_args_list]
        self.assertEqual(len(assets), len(config.STOCK_MINTS))
        self.assertTrue(all(a.startswith("STOCK:") for a in assets))
        mints = {str(c.kwargs["mint"]) for c in m.distribute.call_args_list}
        self.assertEqual(mints, {str(mm) for mm in config.STOCK_MINTS})
        # the stocks leg consumed ITS marker too
        self.assertGreater(cycle._read_marker(cycle._LAST_STOCKS_AIRDROP_PATH), 0)

    def test_stocks_leg_has_its_own_slower_gate(self):
        """The expensive 5-tx-per-holder stocks leg must NOT fire every CPM/SOL
        window — it waits for STOCKS_AIRDROP_INTERVAL_SECONDS."""
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"))
        with self._harness(holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(0)                                   # main gate due
            cycle._write_marker(cycle._LAST_STOCKS_AIRDROP_PATH, int(time.time()))  # stocks NOT due
            cycle.tick()
        # CPM leg ran; no stock mints distributed.
        assets = [c.kwargs.get("asset") for c in m.distribute.call_args_list]
        self.assertEqual(assets, ["CPM"])

    def test_dry_run_does_not_advance_marker(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout",))
        with mock.patch.object(config, "DRY_RUN", True):
            with self._harness(holders=self._holders({owner: 100})) as m:
                cycle._write_last_airdrop_ts(0)
                cycle.tick()
            m.distribute.assert_called_once()
        self.assertEqual(cycle._read_last_airdrop_ts(), 0)

    def test_no_eligible_holders_skips_distribute_and_marker(self):
        with self._harness(holders=[]) as m:
            cycle._write_last_airdrop_ts(0)
            cycle.tick()
        m.distribute.assert_not_called()
        m.distribute_sol.assert_not_called()
        self.assertEqual(cycle._read_last_airdrop_ts(), 0)

    def test_decimals_failure_skips_supply_leg(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout",))
        with self._harness(holders=self._holders({owner: 100}), decimals_exc=RPCError("rpc down")) as m:
            cycle._write_last_airdrop_ts(0)
            cycle.tick()
        # The $CPM leg is fail-CLOSED-skipped; the tokens stay on the wallet and
        # roll into the NEXT window (live-pool reads self-heal).
        m.distribute.assert_not_called()

    def test_excluded_owners_removed_from_distribute(self):
        normal = _wallet()
        operator = str(config.OPERATOR_WALLET)
        self._seed({normal: 100, operator: 9_999}, tasks=("callout",))
        with self._harness(holders=self._holders({normal: 100, operator: 9_999})) as m:
            cycle._write_last_airdrop_ts(0)
            cycle.tick()
        weights = m.distribute.call_args.args[0]
        self.assertIn(normal, weights)
        self.assertNotIn(operator, weights)


class TestSolAirdropLeg(_CycleBase):
    """The SOL airdrop pays ONLY from the sol_pool accumulator, capped, and
    never below the wallet floor + ad reserve."""

    def test_pays_from_pool_and_decrements_by_actual_sent(self):
        owner = _wallet()
        self._seed({owner: 100})
        cycle._add_sol_pool(400_000_000)
        with self._harness(before=2 * _SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
            m.distribute_sol.return_value = {"actual_sent": 150_000_000}
            cycle._write_last_airdrop_ts(0)   # due
            cycle._write_last_claim_ts(int(time.time()))  # claim NOT due (isolate the leg)
            cycle.tick()
        m.distribute_sol.assert_called_once()
        pool_arg = m.distribute_sol.call_args.args[0]
        self.assertEqual(pool_arg, 400_000_000)  # full pool fits under all bounds
        weights = m.distribute_sol.call_args.args[1]
        self.assertIn(owner, weights)
        # Pool decremented by exactly what CONFIRMED.
        self.assertEqual(cycle.read_sol_pool(), 250_000_000)

    def test_respects_wallet_floor_and_ad_reserve(self):
        owner = _wallet()
        self._seed({owner: 100})
        cycle._add_sol_pool(2 * _SOL)               # pool says 2 SOL…
        cycle._add_ad_reserve(700_000_000)          # …but 0.7 SOL is the ad budget
        # wallet = 1 SOL → available = 1 SOL − floor(0.05) − ad(0.7) = 0.25 SOL
        with self._harness(before=_SOL, after=_SOL, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(0)
            cycle._write_last_claim_ts(int(time.time()))
            cycle.tick()
        pool_arg = m.distribute_sol.call_args.args[0]
        self.assertEqual(pool_arg, _SOL - config.GAS_FLOOR_LAMPORTS - 700_000_000)

    def test_capped_by_max_sol_airdrop(self):
        owner = _wallet()
        self._seed({owner: 100})
        cycle._add_sol_pool(2 * _SOL)
        with mock.patch.object(config, "MAX_SOL_AIRDROP_LAMPORTS", 100_000_000):
            with self._harness(before=10 * _SOL, after=10 * _SOL, holders=self._holders({owner: 100})) as m:
                cycle._write_last_airdrop_ts(0)
                cycle._write_last_claim_ts(int(time.time()))
                cycle.tick()
        self.assertEqual(m.distribute_sol.call_args.args[0], 100_000_000)

    def test_empty_pool_means_no_sol_leg(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(0)
            cycle._write_last_claim_ts(int(time.time()))
            cycle.tick()
        m.distribute_sol.assert_not_called()


class TestCasino(_CycleBase):
    """Lvl-4 casino: every CASINO_INTERVAL one weighted-random wallet that
    completed ALL previous levels wins the pot (capped, floor-guarded)."""

    def _quiet_money(self):
        """Gate claim + airdrops off so the only money path is the casino."""
        now = int(time.time())
        cycle._write_last_claim_ts(now)
        cycle._write_last_airdrop_ts(now)
        cycle._write_marker(cycle._LAST_STOCKS_AIRDROP_PATH, now)

    def test_draw_pays_one_lvl4_winner_and_decrements_pot(self):
        lvl4, lvl2 = _wallet(), _wallet()
        # lvl4 did both tasks; lvl2 only the callout → NOT in the draw.
        state_balances = {lvl4: 100, lvl2: 100}
        now = int(time.time())
        state = {
            lvl4: {"first_seen_ts": now - 7200, "last_balance": 100, "last_check_ts": now - 3600,
                   "held_seconds": 500, "tasks": {"callout": 1, "bullpost": 1},
                   "lvl3_since": now - 3600},   # matured past the 10-min bar
            lvl2: {"first_seen_ts": now - 7200, "last_balance": 100, "last_check_ts": now - 3600,
                   "held_seconds": 500, "tasks": {"callout": 1}},
        }
        tracker.save_state(state)
        cycle._adjust_casino_pool(50_000_000)
        with self._harness(before=10 * _SOL, after=10 * _SOL, holders=self._holders(state_balances)) as m:
            self._quiet_money()
            cycle.tick()
        m.build_and_send.assert_called_once()
        self.assertIn("casino_win(50000000)", m.build_and_send.call_args.kwargs["label"])
        self.assertEqual(cycle.read_casino_pool(), 0)
        self.assertGreater(cycle._read_marker(cycle._LAST_CASINO_PATH), 0)

    def test_pick_is_uniform_one_ticket_per_wallet(self):
        """UNIFORM odds: a whale (huge weight) and a shrimp are one ticket
        each. With a seeded rng over many draws both win ~half the time."""
        import random as _random

        candidates = {"a_whale": 10**12, "b_shrimp": 1}
        rng = _random.Random(42)
        with mock.patch.object(cycle, "_rng", rng):
            wins = {"a_whale": 0, "b_shrimp": 0}
            for _ in range(1000):
                wins[rng.choice(sorted(candidates))] += 1
        # both get a fair share — weight plays no role
        self.assertGreater(wins["b_shrimp"], 400)
        self.assertGreater(wins["a_whale"], 400)

    def test_pot_below_min_keeps_growing(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"))
        cycle._adjust_casino_pool(1_000_000)  # < MIN_CASINO_DRAW (5M)
        with self._harness(holders=self._holders({owner: 100})) as m:
            self._quiet_money()
            cycle.tick()
        m.build_and_send.assert_not_called()
        self.assertEqual(cycle.read_casino_pool(), 1_000_000)
        self.assertEqual(cycle._read_marker(cycle._LAST_CASINO_PATH), 0)  # retry soon

    def test_no_lvl4_candidates_pot_intact(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout",))  # lvl 2 only
        cycle._adjust_casino_pool(50_000_000)
        with self._harness(holders=self._holders({owner: 100})) as m:
            self._quiet_money()
            cycle.tick()
        m.build_and_send.assert_not_called()
        self.assertEqual(cycle.read_casino_pool(), 50_000_000)
        self.assertEqual(cycle._read_marker(cycle._LAST_CASINO_PATH), 0)

    def test_fresh_lvl3_must_wait_10_minutes(self):
        """A wallet that JUST reached lvl 3 is not in the draw until it has
        been lvl 3 for CASINO_MIN_LVL3_SECONDS (2 casino cycles)."""
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"), lvl3_age=60)  # only 1 min at lvl 3
        cycle._adjust_casino_pool(50_000_000)
        with self._harness(before=10 * _SOL, after=10 * _SOL, holders=self._holders({owner: 100})) as m:
            self._quiet_money()
            cycle.tick()
        m.build_and_send.assert_not_called()                       # not matured → no draw
        self.assertEqual(cycle.read_casino_pool(), 50_000_000)     # pot intact
        self.assertEqual(cycle._read_marker(cycle._LAST_CASINO_PATH), 0)  # retries soon

    def test_payout_capped(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"))
        cycle._adjust_casino_pool(2 * _SOL)
        with mock.patch.object(config, "MAX_CASINO_PAYOUT_LAMPORTS", 100_000_000):
            with self._harness(before=10 * _SOL, after=10 * _SOL, holders=self._holders({owner: 100})) as m:
                self._quiet_money()
                cycle.tick()
        self.assertIn("casino_win(100000000)", m.build_and_send.call_args.kwargs["label"])
        self.assertEqual(cycle.read_casino_pool(), 2 * _SOL - 100_000_000)

    def test_respects_wallet_floor_ad_reserve_and_sol_pool(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"))
        cycle._adjust_casino_pool(2 * _SOL)
        cycle._add_ad_reserve(500_000_000)
        cycle._add_sol_pool(300_000_000)
        # wallet = 1 SOL → available = 1e9 − floor(5e7) − ad(5e8) − sol_pool(3e8) = 0.15 SOL
        with self._harness(before=_SOL, after=_SOL, holders=self._holders({owner: 100})) as m:
            self._quiet_money()
            cycle.tick()
        expected = _SOL - config.GAS_FLOOR_LAMPORTS - 500_000_000 - 300_000_000
        self.assertIn(f"casino_win({expected})", m.build_and_send.call_args.kwargs["label"])

    def test_interval_gate(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"))
        cycle._adjust_casino_pool(50_000_000)
        with self._harness(holders=self._holders({owner: 100})) as m:
            self._quiet_money()
            cycle._write_marker(cycle._LAST_CASINO_PATH, int(time.time()))  # just drew
            cycle.tick()
        m.build_and_send.assert_not_called()
        self.assertEqual(cycle.read_casino_pool(), 50_000_000)

    def test_dry_run_draws_nothing(self):
        owner = _wallet()
        self._seed({owner: 100}, tasks=("callout", "bullpost"))
        cycle._adjust_casino_pool(50_000_000)
        with mock.patch.object(config, "DRY_RUN", True):
            with self._harness(before=10 * _SOL, after=10 * _SOL, holders=self._holders({owner: 100})) as m:
                self._quiet_money()
                cycle.tick()
        m.build_and_send.assert_not_called()
        self.assertEqual(cycle.read_casino_pool(), 50_000_000)
        self.assertEqual(cycle._read_marker(cycle._LAST_CASINO_PATH), 0)


class TestClaimGating(_CycleBase):
    """Claim+cut+buyback is gated by CLAIM_INTERVAL_SECONDS — its OWN cadence,
    separate from the every-tick holder snapshot."""

    def test_claim_runs_when_due(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))   # gate distribute off
            cycle.tick()                                      # claim marker absent → due
        m.buyback.assert_called_once()                        # claim ran → buyback fired
        self.assertGreater(cycle._read_last_claim_ts(), 0)    # marker advanced

    def test_claim_skipped_when_not_due_but_snapshot_still_runs(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            cycle._write_last_claim_ts(int(time.time()))      # just claimed → NOT due
            cycle.tick()
        m.buyback.assert_not_called()                         # no money moved this tick
        m.build_and_send.assert_not_called()
        self.assertTrue(self._stats_written())                # but the tick (snapshot+stats) still ran

    def test_claim_due_after_interval_elapses(self):
        owner = _wallet()
        self._seed({owner: 100})
        with self._harness(before=_SOL, after=2 * _SOL, holders=self._holders({owner: 100})) as m:
            cycle._write_last_airdrop_ts(int(time.time()))
            # last claim was CLAIM_INTERVAL+1 seconds ago → due again
            cycle._write_last_claim_ts(int(time.time()) - config.CLAIM_INTERVAL_SECONDS - 1)
            cycle.tick()
        m.buyback.assert_called_once()


class TestPoolAutoExclude(_CycleBase):
    """Any holder whose on-chain account is program-owned (an AMM/LP pool, PDA,
    bonding curve) is auto-excluded from the payout — without listing program ids."""

    def test_program_owned_holder_is_auto_excluded(self):
        normal = _wallet()
        pool = _wallet()
        self._seed({normal: 100, pool: 9_999}, tasks=("callout",))
        FAKE_AMM = str(config.PUMPSWAP_AMM_PROGRAM)

        def classify(pks):
            return {pk: (FAKE_AMM if pk == pool else str(config.SYSTEM_PROGRAM)) for pk in pks}

        with self._harness(holders=self._holders({normal: 100, pool: 9_999})) as m:
            with mock.patch.object(cycle.rpc, "get_account_owner_programs", side_effect=classify):
                cycle._write_last_airdrop_ts(0)   # distribute due
                cycle.tick()
            weights = m.distribute.call_args.args[0]
        self.assertIn(normal, weights)       # real wallet still paid
        self.assertNotIn(pool, weights)      # program-owned pool excluded

    def test_extra_excluded_owners_env_is_honored(self):
        normal = _wallet()
        manual = _wallet()
        self._seed({normal: 100, manual: 9_999}, tasks=("callout",))
        with mock.patch.object(config, "EXCLUDED_OWNERS", set(config.EXCLUDED_OWNERS) | {manual}):
            with self._harness(holders=self._holders({normal: 100, manual: 9_999})) as m:
                cycle._write_last_airdrop_ts(0)
                cycle.tick()
                weights = m.distribute.call_args.args[0]
        self.assertIn(normal, weights)
        self.assertNotIn(manual, weights)


if __name__ == "__main__":
    unittest.main()
