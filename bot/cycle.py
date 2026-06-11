"""Main cycle: claim → dev cut → ad reserve → 3-way reward split → 3 airdrops.

Coin Pro Max money flow per claim (claimed = measured wallet delta):
  20% → operator (dev cut, separate wallet)
  20% → ad-bounty reserve: STAYS on this wallet, the bot never spends it
  15% → SOL airdrop pool    → all eligible holders (lvl ≥ 1: buy & hold)
  15% → $CPM buyback pool   → holders who did the pump.fun call-out (lvl 2)
  15% → xStocks basket pool → holders who did the communities bullpost (lvl 3)
  15% → CASINO pot          → every 5 min ONE uniform-random lvl-4 wallet
  Every airdrop is weighted by held_seconds × balance (casino is equal-odds).
  ANY sell resets the accumulated time (start over); task flags are sticky.

SAFETY MODEL (every rule here was paid for in real drained SOL):
  - Every on-chain READ is fail-CLOSED. RPCError → abort that step (or the whole
    tick). NEVER assume zero, NEVER continue on partial state. A network blip
    must never be read as "nobody holds anything" (that would mass-reset
    held_seconds) or "the wallet is empty".
  - `claimed = wallet_sol_after − wallet_sol_before`. The pump.fun program
    return value is NEVER trusted (Jobcoin). Every split is sized off this
    measured delta, not off any quote.
  - The operator cut runs FIRST, before any reward spending.
  - Hard caps are the last line of defense: MAX_BUYBACK_LAMPORTS,
    MAX_STOCK_BASKET_LAMPORTS, MAX_SOL_AIRDROP_LAMPORTS. Even with broken math
    upstream a single tick cannot spend more than a cap on any leg. Excess
    stays in the wallet / pool (no carryover for swaps; the SOL pool is an
    explicit accumulator).
  - The SOL airdrop pays ONLY from the sol_pool accumulator (fed exclusively by
    measured claim deltas, decremented by confirmed sends) and additionally
    never lets the wallet drop below GAS_FLOOR + the ad-bounty reserve.
  - Task detection (pump.fun thread / communities feed) is OUTBOUND polling,
    read-only, fail-SAFE, throttled. No inbound endpoint can trigger anything.
  - DRY_RUN: claims/cut/swaps are built and logged but never submitted, so the
    measured delta is 0 and the money branch is a no-op; the airdrops compute
    real plans against live pools and log without sending.
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any

from solders.pubkey import Pubkey

from . import (
    community,
    config,
    distribute as dist,
    loyalty_tracker as tracker,
    pumpfun,
    pumpthread,
    rpc,
    solana_tx as stx,
    stocks,
    swap,
)
from .rpc import RPCError


# -------- state markers --------

_LAST_AIRDROP_PATH = f"{config.DATA_DIR}/last_airdrop_at.txt"
_LAST_STOCKS_AIRDROP_PATH = f"{config.DATA_DIR}/last_stocks_airdrop_at.txt"
_LAST_CASINO_PATH = f"{config.DATA_DIR}/last_casino_at.txt"
_LAST_CLAIM_PATH = f"{config.DATA_DIR}/last_claim_at.txt"

# Casino randomness — OS entropy; tests patch this with a seeded Random.
_rng = random.SystemRandom()

# Throttle for the external task APIs (allowlist files are read every tick).
_last_tasks_fetch_ts = 0


def _refresh_tasks(state: dict, now: int) -> None:
    """Sticky-mark task completion from the allowlists (every tick) + the two
    external feeds (throttled): pump.fun thread comments → "callout",
    coin-communities posts → "bullpost". Fail-SAFE: any failure just skips that
    refresh and keeps existing flags — a flaky API never revokes a task.
    """
    callout: set[str] = set()
    bullpost: set[str] = set()
    try:
        callout |= pumpthread.load_allowlist()
        bullpost |= community.load_allowlist()
    except Exception as exc:  # never let allowlist IO break the tick
        print(f"[cycle] task allowlist read failed (keeping flags): {exc}")

    global _last_tasks_fetch_ts
    if now - _last_tasks_fetch_ts >= config.TASKS_REFRESH_SECONDS:
        _last_tasks_fetch_ts = now  # advance even on failure: don't hammer a down API
        try:
            callout |= pumpthread.fetch_callout_wallets(str(config.LOYALTY_MINT))
        except pumpthread.PumpThreadError as exc:
            print(f"[cycle] pump.fun thread fetch failed (fail-SAFE, keeping flags): {exc}")
        try:
            bullpost |= community.fetch_engaged_from_api(str(config.LOYALTY_MINT))
        except community.CommunityError as exc:
            print(f"[cycle] community API fetch failed (fail-SAFE, keeping flags): {exc}")

    if callout:
        tracker.apply_task(state, callout, "callout", now=now)
    if bullpost:
        tracker.apply_task(state, bullpost, "bullpost", now=now)


# -------- SOL-pool + ad-reserve accumulators --------

def _read_counter(path: str, key: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return max(0, int(json.load(f).get(key, 0)))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return 0


def _write_counter(path: str, key: str, value: int) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({key: max(0, int(value))}, f)
    os.replace(tmp, path)


def read_sol_pool() -> int:
    return _read_counter(config.SOL_POOL_PATH, "lamports")


def _add_sol_pool(lamports: int) -> None:
    _write_counter(config.SOL_POOL_PATH, "lamports", read_sol_pool() + lamports)


def _sub_sol_pool(lamports: int) -> None:
    _write_counter(config.SOL_POOL_PATH, "lamports", read_sol_pool() - lamports)


def read_ad_reserve() -> int:
    """Outstanding ad-bounty budget (lamports) sitting on the bot wallet. The
    bot only ever INCREASES this; when the operator withdraws/spends ad money
    they decrement /data/ad_reserve.json by hand (documented in README)."""
    return _read_counter(config.AD_RESERVE_PATH, "lamports")


def _add_ad_reserve(lamports: int) -> None:
    _write_counter(config.AD_RESERVE_PATH, "lamports", read_ad_reserve() + lamports)


def read_cpm_buy_pool() -> int:
    return _read_counter(config.CPM_BUY_POOL_PATH, "lamports")


def _adjust_cpm_buy_pool(delta: int) -> None:
    _write_counter(config.CPM_BUY_POOL_PATH, "lamports", read_cpm_buy_pool() + delta)


def read_stock_buy_pool() -> int:
    return _read_counter(config.STOCK_BUY_POOL_PATH, "lamports")


def _adjust_stock_buy_pool(delta: int) -> None:
    _write_counter(config.STOCK_BUY_POOL_PATH, "lamports", read_stock_buy_pool() + delta)


def read_casino_pool() -> int:
    return _read_counter(config.CASINO_POOL_PATH, "lamports")


def _adjust_casino_pool(delta: int) -> None:
    _write_counter(config.CASINO_POOL_PATH, "lamports", read_casino_pool() + delta)


def read_total_distributed() -> int:
    """Lifetime SOL spent on holders (airdrops + casino + buybacks + baskets)."""
    return _read_counter(config.TOTAL_DISTRIBUTED_PATH, "lamports")


def _add_total_distributed(lamports: int) -> None:
    if lamports > 0:
        _write_counter(config.TOTAL_DISTRIBUTED_PATH, "lamports", read_total_distributed() + lamports)


def _casino_draw(candidates: dict[str, int], now: int) -> None:
    """Lvl-4 casino: pay the whole pool (capped) to ONE random wallet that
    completed all previous levels — UNIFORM odds: every eligible wallet is one
    ticket, regardless of size (operator's explicit choice; eligibility itself
    is the anti-sybil bar — each wallet needs MIN_HOLDING plus its own pump.fun
    call-out and community bullpost). Money rules mirror the SOL airdrop: pays
    ONLY from the casino accumulator, never below the wallet floor + the ad
    reserve + the sol-airdrop pool, decrement only on successful submit.
    """
    pool = read_casino_pool()
    if pool < config.MIN_CASINO_DRAW_LAMPORTS:
        return  # pot too small — keep growing, retry next tick
    if not candidates:
        print("[cycle] casino due but nobody at lvl 4 yet — pot keeps growing")
        return

    try:
        balance = rpc.get_sol_balance(str(config.WALLET_PUBKEY))
    except RPCError as exc:
        print(f"[cycle] casino balance read failed (fail-CLOSED, skipping draw): {exc}")
        return
    available = balance - config.GAS_FLOOR_LAMPORTS - read_ad_reserve() - read_sol_pool()
    payout = min(pool, config.MAX_CASINO_PAYOUT_LAMPORTS, max(0, available))
    if payout <= 0:
        print(f"[cycle] casino skipped: pool={pool} but available={available}")
        return

    winner = _rng.choice(sorted(candidates))  # sorted → reproducible under a seeded test rng
    if config.DRY_RUN:
        print(f"[cycle] DRY_RUN casino: would pay {payout} lamports to {winner} "
              f"({len(candidates)} tickets in the draw)")
        return

    try:
        sig = stx.build_and_send(
            [stx.ix_transfer_sol(
                from_pubkey=config.WALLET_PUBKEY,
                to_pubkey=Pubkey.from_string(winner),
                lamports=payout,
            )],
            label=f"casino_win({payout})",
        )
    except (RPCError, ValueError) as exc:
        print(f"[cycle] casino payout failed (pot stays, retry next window): {exc}")
        return

    _adjust_casino_pool(-payout)
    _add_total_distributed(payout)
    dist._append_payouts_log([{
        "ts": now, "asset": "CASINO", "wallet": winner, "amount": payout,
        "sig": sig, "confirmed": True,
    }])
    print(f"[cycle] CASINO: {winner} won {payout} lamports ({len(candidates)} tickets)")
    _write_marker(_LAST_CASINO_PATH, now)


def _read_marker(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def _write_marker(path: str, ts: int) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(ts))
    os.replace(tmp, path)


def _read_last_airdrop_ts() -> int:
    return _read_marker(_LAST_AIRDROP_PATH)


def _write_last_airdrop_ts(ts: int) -> None:
    _write_marker(_LAST_AIRDROP_PATH, ts)


def _read_last_claim_ts() -> int:
    return _read_marker(_LAST_CLAIM_PATH)


def _write_last_claim_ts(ts: int) -> None:
    _write_marker(_LAST_CLAIM_PATH, ts)


# -------- pool auto-detection (never pay an AMM/LP pool) --------

_pool_owners: set[str] = set()        # discovered program-owned holders (sticky)
_classified_owners: set[str] = set()  # owners we've already looked up


def _excluded_owners(live_owners) -> set[str]:
    """The full never-pay set: config.EXCLUDED_OWNERS (bot/operator/manual list)
    PLUS auto-detected pools. A holder whose on-chain account is owned by a
    PROGRAM (not the System Program) is a pool / PDA / bonding curve, never a real
    wallet — so it's excluded. Classification is cached (sticky) and fail-SAFE: an
    RPC blip just skips classifying NEW owners this tick; everything already known
    stays excluded.
    """
    base = set(config.EXCLUDED_OWNERS) | _pool_owners
    if not config.AUTODETECT_POOLS:
        return base
    unknown = [o for o in live_owners if o not in _classified_owners and o not in base]
    if unknown:
        try:
            owners = rpc.get_account_owner_programs(unknown)
        except RPCError as exc:
            print(f"[cycle] pool auto-detect skipped this tick (will retry): {exc}")
            return base
        sysprog = str(config.SYSTEM_PROGRAM)
        for pk, prog in owners.items():
            _classified_owners.add(pk)
            if prog is not None and prog != sysprog:
                _pool_owners.add(pk)
                print(f"[cycle] auto-excluded pool/PDA holder {pk} (account owned by {prog})")
    return set(config.EXCLUDED_OWNERS) | _pool_owners


def _write_stats(d: dict[str, Any]) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = f"{config.STATS_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, config.STATS_PATH)


# -------- snapshot --------

def _snapshot_loyalty_holders(token_program: str) -> dict[str, int]:
    """All wallets currently holding $LOYALTY, keyed by OWNER pubkey.

    Owner-keyed (not token-account-keyed): the tracker tracks people, and one
    owner may hold the same mint across several token accounts (rare). We sum
    them so a split balance still counts as one holder.

    Raises RPCError on any failure — the caller treats this as fail-CLOSED and
    skips the whole tick rather than feeding `tracker.update` a partial snapshot
    (a partial snapshot would falsely reset everyone who is missing).
    """
    raw = rpc.get_token_holders(str(config.LOYALTY_MINT), program=token_program)
    by_owner: dict[str, int] = {}
    for entry in raw:
        owner = entry["owner"]
        by_owner[owner] = by_owner.get(owner, 0) + int(entry["amount"])
    return by_owner


# -------- claim → operator cut → buyback --------

def _confirm_signatures(sigs: list, timeout_s: float = 20.0) -> None:
    """Block until each claim signature lands ('confirmed'/'finalized') before we
    measure the wallet delta.

    `send_raw_tx` returns on SUBMIT, not on confirmation. Reading the wallet
    balance immediately after the claim therefore sees the just-credited creator
    fee as 0, so `claimed = after - before` is ~0 and the buyback is wrongly
    skipped (the fee then just sits in the wallet). Waiting here is the fix for
    "it claims fees but never buys back". Best-effort: on timeout or an RPC blip
    we proceed anyway and the balance read reflects whatever has landed so far.
    """
    pending = {s for s in sigs if s}
    if not pending:
        return
    deadline = time.time() + timeout_s
    while pending and time.time() < deadline:
        keys = list(pending)
        try:
            statuses = rpc.get_signature_statuses(keys)
        except RPCError:
            return
        for sig, st in zip(keys, statuses):
            # done == landed (confirmed/finalized) OR failed (err set: it will
            # never deliver a fee, so stop waiting on it).
            if st and (st.get("confirmationStatus") in ("confirmed", "finalized") or st.get("err") is not None):
                pending.discard(sig)
        if pending:
            time.sleep(1.0)


def _claim_cut_split() -> dict[str, int]:
    """Claim creator fees, pay the dev cut, book the ad reserve, then split the
    reward pool three ways: SOL-airdrop accumulator / $CPM buyback / xStocks
    basket.

    All money movement here is best-effort and self-isolating: any failure
    leaves the SOL in the wallet for the next cycle (the measured delta self-
    heals). Reads are fail-CLOSED. Returns a small dict for logging.
    """
    out = {"claimed": 0, "operator": 0, "ad_reserve": 0, "casino": 0, "sol_pool": 0, "buyback": 0, "stocks": 0}
    wallet = str(config.WALLET_PUBKEY)

    try:
        before = rpc.get_sol_balance(wallet)
    except RPCError as exc:
        print(f"[cycle] balance read (before claim) failed, skipping money steps: {exc}")
        return out

    # Two independent claims; each logs + swallows its own failure so one can't
    # block the other. Pre-migration coins only have bonding-curve fees; the AMM
    # claim self-skips when there is no creator-vault ATA yet.
    sig_bond = pumpfun.claim_bonding_curve()
    sig_amm = pumpfun.claim_amm()
    # CRITICAL: wait for the claims to LAND before measuring the delta. send is
    # fire-and-forget (returns on submit), so an immediate balance read misses
    # the just-credited fee → claimed reads ~0 → the split is wrongly skipped.
    # No threshold: any positive delta, however small, flows to the split below.
    _confirm_signatures([sig_bond, sig_amm])

    try:
        after = rpc.get_sol_balance(wallet)
    except RPCError as exc:
        print(f"[cycle] balance read (after claim) failed, skipping split: {exc}")
        return out

    claimed = after - before
    out["claimed"] = claimed
    if claimed <= 0:
        # Nothing collected (empty vault) — claim tx fees may net slightly
        # negative; either way there is nothing to split this tick.
        if config.DRY_RUN:
            print("[cycle] DRY_RUN: claims were build-only, so measured delta is 0 (expected).")
        else:
            print(f"[cycle] claimed={claimed} lamports — nothing to split this tick")
        return out

    # 1) Operator (dev) cut FIRST so the operator is paid even if anything
    #    downstream fails.
    operator_lamports = int(claimed * config.OPERATOR_PCT)
    if operator_lamports > 0:
        try:
            stx.build_and_send(
                [stx.ix_transfer_sol(
                    from_pubkey=config.WALLET_PUBKEY,
                    to_pubkey=config.OPERATOR_WALLET,
                    lamports=operator_lamports,
                )],
                label=f"operator_cut({operator_lamports})",
            )
            out["operator"] = operator_lamports
        except RPCError as exc:
            # Don't touch the reward pool if we couldn't pay the operator — the
            # SOL stays put and the next cycle re-measures (delta ~0 next tick,
            # so it accumulates safely rather than mis-distributing).
            print(f"[cycle] operator cut failed, skipping split this tick: {exc}")
            return out

    # 2) Ad-bounty reserve: NO transfer — the SOL stays on this wallet as the
    #    operator's manual ad budget. We only book it so the SOL airdrop can
    #    never pay it out and the site can show it.
    ad_lamports = int(claimed * config.AD_RESERVE_PCT)
    if ad_lamports > 0:
        _add_ad_reserve(ad_lamports)
        out["ad_reserve"] = ad_lamports

    # 2b) Casino pot (lvl 4): accumulates; paid out by _casino_draw on its own
    #     5-minute gate to ONE uniform-random lvl-4 wallet.
    casino_lamports = int(claimed * config.CASINO_PCT)
    if casino_lamports > 0:
        _adjust_casino_pool(casino_lamports)
        out["casino"] = casino_lamports

    # 3) Per-level reward shares (explicit percentages). The last leg takes the
    #    integer-truncation remainder so the split always sums to `claimed`
    #    exactly. Swaps fire only once a pool clears MIN_SWAP_LAMPORTS, so
    #    micro-claims build up instead of being burned on tx overhead.
    sol_share = int(claimed * config.SOL_AIRDROP_PCT)
    supply_share = int(claimed * config.SUPPLY_PCT)
    stocks_share = claimed - operator_lamports - ad_lamports - casino_lamports - sol_share - supply_share
    if sol_share <= 0 and supply_share <= 0 and stocks_share <= 0:
        return out

    # 3a) SOL airdrop accumulator (paid out on the airdrop gate, capped there).
    if sol_share > 0:
        _add_sol_pool(sol_share)
        out["sol_pool"] = sol_share
    if supply_share > 0:
        _adjust_cpm_buy_pool(supply_share)
    if stocks_share > 0:
        _adjust_stock_buy_pool(stocks_share)

    # 3b) $CPM buyback from its pool, hard-capped. Decrement ONLY on a
    #     successful submit — a failed swap leaves the budget pooled for retry.
    buy_pool = read_cpm_buy_pool()
    if buy_pool >= config.MIN_SWAP_LAMPORTS:
        buyback_lamports = min(buy_pool, config.MAX_BUYBACK_LAMPORTS)
        if swap.buyback(buyback_lamports) is not None:
            _adjust_cpm_buy_pool(-buyback_lamports)
            _add_total_distributed(buyback_lamports)
            out["buyback"] = buyback_lamports

    # 3c) xStocks basket from its pool, hard-capped (module re-caps too).
    #     Decrement by the legs that actually submitted; failed legs stay pooled.
    stock_pool = read_stock_buy_pool()
    if stock_pool >= config.MIN_SWAP_LAMPORTS:
        basket_lamports = min(stock_pool, config.MAX_STOCK_BASKET_LAMPORTS)
        sigs = stocks.buy_basket(basket_lamports)
        if sigs:
            per_leg = basket_lamports // len(config.STOCK_MINTS)
            spent = per_leg * len(sigs)
            _adjust_stock_buy_pool(-spent)
            _add_total_distributed(spent)
            out["stocks"] = spent

    print(
        f"[cycle] claimed={claimed} operator={out['operator']} ad_reserve={out['ad_reserve']} "
        f"casino+={out['casino']} sol_pool+={out['sol_pool']} buyback={out['buyback']} "
        f"stocks={out['stocks']} lamports"
    )
    return out


# -------- main tick --------

def tick() -> None:
    """One bot cycle. Called every CYCLE_INTERVAL_SECONDS."""
    now = int(time.time())

    # 0. Detect the mint's token program ONCE (SPL vs Token-2022). It's part of
    #    the ATA seed, so guessing wrong yields phantom-zero balances. Reused for
    #    both the holder snapshot and the distribute transfers. Fail-CLOSED.
    try:
        token_program = rpc.get_token_program(str(config.LOYALTY_MINT))
    except RPCError as exc:
        print(f"[cycle] token-program detect failed (fail-CLOSED, skipping tick): {exc}")
        return

    # 1. Update the held-seconds tracker (the coin's heart). MUST run every tick.
    try:
        live = _snapshot_loyalty_holders(token_program)  # RPCError → skip tick
    except RPCError as exc:
        print(f"[cycle] holder snapshot failed (fail-CLOSED, skipping tick): {exc}")
        return

    state = tracker.load_state()
    tracker.update(state, live, now=now)
    # 1b. Task detection (pump.fun call-outs + community bullposts) — sticky
    #     flags, fail-SAFE, throttled. Marks before we persist + compute weights.
    _refresh_tasks(state, now)
    # 1c. Stamp/clear the lvl-3 clock (lvl-4 entry requires 10 continuous
    #     minutes at lvl 3) — after tasks, before persist.
    tracker.stamp_lvl3(state, now=now)
    tracker.save_state(state)

    # 2. Stats for the read-only web side (/api/stats). Exclusions = static set
    #    (bot/operator/manual) + auto-detected pools, so no AMM/LP pool is ever
    #    paid out or shown as a holder.
    excluded = _excluded_owners(live.keys())
    weights = tracker.filter_excluded(tracker.eligible_weights(state), excluded)
    payout_weights = tracker.filter_excluded(tracker.weighted_holdings(state), excluded)
    callout_weights = tracker.filter_excluded(tracker.task_weights(state, "callout"), excluded)
    bullpost_weights = tracker.filter_excluded(tracker.task_weights(state, "bullpost"), excluded)
    casino_candidates = tracker.filter_excluded(tracker.casino_weights(state, now=now), excluded)
    last_airdrop = _read_last_airdrop_ts()
    _write_stats(
        {
            "ts": now,
            "wallet": str(config.WALLET_PUBKEY),
            "mint": str(config.LOYALTY_MINT),
            "eligible_holders": len(weights),
            "total_weight_seconds": sum(weights.values()),
            "min_holding_raw": config.MIN_HOLDING_RAW,
            # Published so the key-less web server can mirror the engine's
            # exclusion set (bot wallet, operator, AMM pools) without importing
            # bot.config.
            "excluded_owners": sorted(excluded),
            "last_airdrop_ts": last_airdrop,
            "next_airdrop_ts": last_airdrop + config.AIRDROP_INTERVAL_SECONDS,
            "dry_run": config.DRY_RUN,
            # CPM extras: level tiers + pools for the site.
            "lvl1_holders": len(payout_weights),
            "lvl_callout_holders": len(callout_weights),
            "lvl_bullpost_holders": len(bullpost_weights),
            "lvl4_casino_tickets": len(casino_candidates),
            "sol_pool_lamports": read_sol_pool(),
            "ad_reserve_lamports": read_ad_reserve(),
            "cpm_buy_pool_lamports": read_cpm_buy_pool(),
            "stock_buy_pool_lamports": read_stock_buy_pool(),
            "casino_pool_lamports": read_casino_pool(),
            "total_distributed_lamports": read_total_distributed(),
            "next_casino_ts": _read_marker(_LAST_CASINO_PATH) + config.CASINO_INTERVAL_SECONDS,
            "stock_mints": [str(m) for m in config.STOCK_MINTS],
        }
    )

    # 3-5. Claim creator fees → dev cut → ad reserve → 3-way split. Gated by
    #      CLAIM_INTERVAL_SECONDS so it runs on its OWN cadence (not every tick):
    #      the snapshot above runs every tick (fast sell-detection), while money
    #      moves less often to batch fees and save gas. Marker advances even under
    #      DRY_RUN (the claim is build-only then), so the cadence is identical.
    last_claim = _read_last_claim_ts()
    if now - last_claim >= config.CLAIM_INTERVAL_SECONDS:
        _claim_cut_split()
        _write_last_claim_ts(now)

    # 5b. Casino draw — its OWN fast gate (one winner = one tx, so a 5-minute
    #     cadence is cheap). Runs regardless of the airdrop gate below.
    if now - _read_marker(_LAST_CASINO_PATH) >= config.CASINO_INTERVAL_SECONDS:
        _casino_draw(casino_candidates, now)

    # 6. Airdrops — gated separately so payouts are batched. Three legs, each
    #    computed against its LIVE on-wallet pool (never a DB sum).
    if now - last_airdrop < config.AIRDROP_INTERVAL_SECONDS:
        return
    if not payout_weights:
        # Nobody eligible yet — don't burn the window; retry next tick so the
        # first qualifying holders get paid promptly once they cross the bar.
        print("[cycle] airdrop due but no eligible holders yet — waiting")
        return

    # 6a. SOL airdrop → every eligible holder (lvl ≥ 1). Pays ONLY from the
    #     sol_pool accumulator, hard-capped per window, and never lets the
    #     wallet dip below GAS_FLOOR + the ad-bounty reserve. Balance read is
    #     fail-CLOSED → skip just this leg.
    sol_pool = read_sol_pool()
    if sol_pool > 0:
        try:
            balance = rpc.get_sol_balance(str(config.WALLET_PUBKEY))
            # Floor + ad reserve + the casino pot all live on this wallet too —
            # the SOL airdrop must never spend their share (and vice versa).
            available = balance - config.GAS_FLOOR_LAMPORTS - read_ad_reserve() - read_casino_pool()
            to_pay = min(sol_pool, config.MAX_SOL_AIRDROP_LAMPORTS, max(0, available))
            if to_pay > 0:
                res = dist.distribute_sol(to_pay, payout_weights)
                actual = int(res.get("actual_sent", 0))
                if actual > 0:
                    _sub_sol_pool(actual)
                    _add_total_distributed(actual)
            else:
                print(f"[cycle] SOL airdrop skipped: pool={sol_pool} but available={available}")
        except RPCError as exc:
            print(f"[cycle] SOL airdrop balance read failed (fail-CLOSED, skipping leg): {exc}")

    # 6b. $CPM supply airdrop → holders who did the pump.fun call-out. Dust
    #     floor keeps fee burn sane; skipped dust rolls into the next window.
    try:
        decimals = rpc.get_token_decimals(str(config.LOYALTY_MINT))
        if callout_weights:
            dist.distribute(
                callout_weights,
                token_program=Pubkey.from_string(token_program),
                decimals=decimals,
                asset="CPM",
                min_payout=config.MIN_CPM_PAYOUT_RAW,
            )
        else:
            print("[cycle] supply airdrop: nobody with a call-out yet")
    except RPCError as exc:
        print(f"[cycle] supply airdrop failed (fail-CLOSED, skipping leg): {exc}")

    # 6c. xStocks airdrop → holders who did the communities bullpost. The
    #     EXPENSIVE leg (5 txs per holder + ~0.002 SOL ATA rent per stock for a
    #     first-time recipient) → its own, slower gate. A failed mint skips
    #     just itself; tokens roll into the next stocks window.
    last_stocks = _read_marker(_LAST_STOCKS_AIRDROP_PATH)
    if now - last_stocks >= config.STOCKS_AIRDROP_INTERVAL_SECONDS:
        if bullpost_weights:
            for stock_mint in config.STOCK_MINTS:
                try:
                    sp = rpc.get_token_program(str(stock_mint))
                    sd = rpc.get_token_decimals(str(stock_mint))
                    dist.distribute(
                        bullpost_weights,
                        token_program=Pubkey.from_string(sp),
                        decimals=sd,
                        mint=stock_mint,
                        asset=f"STOCK:{str(stock_mint)[:6]}",
                        min_payout=config.MIN_STOCK_PAYOUT_RAW,
                    )
                except RPCError as exc:
                    print(f"[cycle] stock {stock_mint} airdrop failed (skipping this mint): {exc}")
            if not config.DRY_RUN:
                _write_marker(_LAST_STOCKS_AIRDROP_PATH, now)
        else:
            print("[cycle] stocks airdrop: nobody with a bullpost yet")

    # Advance the marker on a real run only — a DRY_RUN must be repeatable and
    # must never consume the airdrop window.
    if not config.DRY_RUN:
        _write_last_airdrop_ts(now)
