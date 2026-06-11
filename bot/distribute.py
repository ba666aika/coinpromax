"""Distribute $LOYALTY by RANK of held_seconds (relative loyalty, not pro-rata).

Layers:
  * `rank_weights` (in bot.ranking, re-exported here) — PURE. Maps held_seconds
    → rank-based weights via a 1/sqrt(rank) curve. Share depends only on your
    POSITION among holders (longest = #1), not on the absolute seconds. cycle.py
    applies this before `distribute`; held_seconds itself stays the value shown
    in stats / the bubble map. It lives in bot.ranking so the key-less web server
    can import the same curve without pulling in bot.config.
  * `compute_payouts` — PURE integer math. Floor division, remainder kept as
    leftover. No holder is ever paid more than their exact share; the sum of
    payouts can never exceed the pool. This is the part proven by unit + stress
    tests (10k holders). It splits by WHATEVER weights it's given, so it's
    unchanged by the rank curve.
  * `distribute` — the I/O layer. Reads the live on-wallet pool (fail-CLOSED),
    builds one CreateIdempotent+TransferChecked tx per recipient, and sends them
    429-resiliently: one blockhash per chunk, bounded concurrency, send-without-
    confirm, then a bulk getSignatureStatuses sweep to tally what actually landed.

`pool` is ALWAYS the live on-wallet balance, never a DB sum — so any drift
(an in-flight tx we stopped polling) self-heals next cycle.
"""
from __future__ import annotations

import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from solders.pubkey import Pubkey

from . import config, rpc, solana_tx as stx
from .ranking import rank_weights  # noqa: F401  (re-exported: cycle.py uses dist.rank_weights)
from .rpc import RPCError

_SIG_STATUS_BATCH = 256
_SEND_MAX_RETRIES = 8


def compute_payouts(pool: int, weights: dict[str, int]) -> tuple[list[tuple[str, int]], int]:
    """Split `pool` raw units across `weights` by floor(pool * w / total_w).

    Returns (payouts, leftover) where payouts is a list of (wallet, amount) with
    amount > 0, sorted by descending weight (deterministic), and
    leftover = pool - sum(amounts) >= 0.

    Guarantees (enforced by tests):
      - sum(amounts) <= pool         (never over-distribute)
      - every amount <= its exact real share (floor)
      - leftover >= 0
    """
    # Denominator over POSITIVE weights only — same set the loop pays out. If a
    # stray non-positive weight slipped in, including it would shrink total_w
    # and let a real holder's floor share exceed the pool (over-distribution).
    total_w = sum(w for w in weights.values() if w > 0)
    if pool <= 0 or total_w <= 0:
        return [], max(0, pool)
    payouts: list[tuple[str, int]] = []
    distributed = 0
    for wallet, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])):
        if w <= 0:
            continue
        amount = pool * w // total_w
        if amount <= 0:
            continue
        payouts.append((wallet, amount))
        distributed += amount
    return payouts, pool - distributed


def _bot_ata(mint: Pubkey, token_program: Pubkey) -> Pubkey:
    return stx.derive_ata(config.WALLET_PUBKEY, mint, token_program)


def _build_transfer_tx(
    *, recipient: Pubkey, amount: int, mint: Pubkey, token_program: Pubkey, decimals: int, blockhash: str
) -> str:
    source = _bot_ata(mint, token_program)
    dest = stx.derive_ata(recipient, mint, token_program)
    ixs = stx.priority_fee_ixs() + [
        # Idempotent: a no-op when the recipient already has an ATA for this
        # mint. For $CPM they always do (they hold it); for a stock mint this
        # CREATES the account — the rent (~0.002 SOL) is paid by the bot, which
        # is the cost of delivering a new asset to a wallet.
        stx.ix_create_idempotent_ata(config.WALLET_PUBKEY, recipient, mint, token_program),
        stx.ix_transfer_checked(
            source=source,
            mint=mint,
            dest=dest,
            owner=config.WALLET_PUBKEY,
            amount=amount,
            decimals=decimals,
            token_program=token_program,
        ),
    ]
    return stx.build_tx_b64(ixs, blockhash)


def _send_with_backoff(b64_tx: str, *, label: str) -> Optional[str]:
    """Send one tx, retrying on transient RPC failures (429 / timeouts) with
    exponential backoff + jitter. Returns the signature or None if it never
    landed (its tokens stay in the wallet and are re-read live next cycle).
    """
    delay = 0.5
    for attempt in range(_SEND_MAX_RETRIES):
        try:
            return rpc.send_raw_tx(b64_tx)
        except RPCError as exc:
            if attempt == _SEND_MAX_RETRIES - 1:
                print(f"[distribute] {label} gave up after {attempt + 1} tries: {exc}")
                return None
            time.sleep(delay + random.uniform(0, delay * 0.5))
            delay = min(delay * 2, 8.0)
    return None


def _confirm(signatures: list[str]) -> set[str]:
    """Bulk-confirm. Returns the set of signatures that landed without error.
    A few polling rounds (statuses lag right after send). Best-effort: anything
    still unconfirmed is treated as not-sent for accounting (conservative)."""
    confirmed: set[str] = set()
    pending = list(signatures)
    for _ in range(5):
        if not pending:
            break
        still: list[str] = []
        for i in range(0, len(pending), _SIG_STATUS_BATCH):
            batch = pending[i : i + _SIG_STATUS_BATCH]
            try:
                statuses = rpc.get_signature_statuses(batch)
            except RPCError as exc:
                print(f"[distribute] status batch failed (will retry): {exc}")
                still.extend(batch)
                continue
            for sig, st in zip(batch, statuses):
                if st is None:
                    still.append(sig)
                elif st.get("err") is None and st.get("confirmationStatus") in ("confirmed", "finalized"):
                    confirmed.add(sig)
                elif st.get("err") is not None:
                    pass  # failed on-chain — leave out of confirmed
                else:
                    still.append(sig)
        pending = still
        if pending:
            time.sleep(2.0)
    return confirmed


def _append_payouts_log(records: list[dict]) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.PAYOUTS_PATH, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def distribute(
    weights: dict[str, int],
    *,
    token_program: Pubkey,
    decimals: int,
    mint: Pubkey | None = None,
    asset: str = "CPM",
    min_payout: int = 0,
) -> dict:
    """Distribute the live on-wallet pool of `mint` (default: the coin's own
    mint) across `weights`. `asset` is a short label for logs/payout records —
    the same machinery serves the $CPM supply airdrop and each xStocks leg.

    `min_payout` (raw units) is the dust floor: a payout below it costs more in
    tx fees (~55k lamports, plus ~0.002 SOL ATA rent for a first-time stock
    recipient) than it delivers. Skipped dust is NOT lost — it stays in the
    live pool and rolls into the next window.

    This function is agnostic to how the weights were derived; it just splits
    the pool by them. Returns a summary dict. Under DRY_RUN nothing is
    signed/sent — the planned payouts are computed against the real live pool
    and logged.
    """
    mint = mint if mint is not None else config.LOYALTY_MINT
    # Pool = live on-wallet balance. Fail-CLOSED: if we can't read it, abort —
    # distributing against a guessed pool is how money goes sideways.
    pool = rpc.get_spl_balance(str(_bot_ata(mint, token_program)))
    payouts, leftover = compute_payouts(pool, weights)
    if min_payout > 0:
        skipped_dust = sum(a for _, a in payouts if a < min_payout)
        payouts = [(w, a) for w, a in payouts if a >= min_payout]
        leftover += skipped_dust
    summary = {
        "ts": int(time.time()),
        "asset": asset,
        "mint": str(mint),
        "pool": pool,
        "total_weight": sum(weights.values()),
        "recipients": len(payouts),
        "leftover_planned": leftover,
    }

    if not payouts:
        print(f"[distribute] {asset}: nothing to send (pool={pool}, recipients=0)")
        return {**summary, "sent": 0, "actual_sent": 0, "confirmed": 0}

    if config.DRY_RUN:
        preview = payouts[:10]
        print(f"[distribute] DRY_RUN {asset}: pool={pool} → {len(payouts)} recipients, leftover={leftover}")
        for w, a in preview:
            print(f"[distribute]   DRY_RUN {w} <= {a}")
        if len(payouts) > len(preview):
            print(f"[distribute]   … +{len(payouts) - len(preview)} more")
        return {**summary, "sent": 0, "actual_sent": 0, "confirmed": 0, "dry_run": True}

    # Send: chunk by chunk, one blockhash per chunk, bounded concurrency.
    chunk = max(1, config.DISTRIBUTE_CHUNK_SIZE)
    workers = max(1, config.DISTRIBUTE_CONCURRENCY)
    sent: list[tuple[str, str, int]] = []  # (sig, wallet, amount)

    for start in range(0, len(payouts), chunk):
        group = payouts[start : start + chunk]
        try:
            blockhash = rpc.get_recent_blockhash()  # fail-CLOSED per chunk
        except RPCError as exc:
            print(f"[distribute] blockhash fetch failed, stopping early: {exc}")
            break

        def _do(item: tuple[str, int]) -> Optional[tuple[str, str, int]]:
            wallet, amount = item
            try:
                b64 = _build_transfer_tx(
                    recipient=Pubkey.from_string(wallet),
                    amount=amount,
                    mint=mint,
                    token_program=token_program,
                    decimals=decimals,
                    blockhash=blockhash,
                )
            except ValueError as exc:
                print(f"[distribute] bad recipient {wallet}: {exc}")
                return None
            sig = _send_with_backoff(b64, label=f"{asset}→{wallet}")
            return (sig, wallet, amount) if sig else None

        with ThreadPoolExecutor(max_workers=workers) as pool_exec:
            for res in pool_exec.map(_do, group):
                if res:
                    sent.append(res)

    confirmed_sigs = _confirm([s for s, _, _ in sent])
    actual_sent = sum(a for s, _, a in sent if s in confirmed_sigs)

    now = int(time.time())
    _append_payouts_log(
        [
            {"ts": now, "asset": asset, "wallet": w, "amount": a, "sig": s, "confirmed": s in confirmed_sigs}
            for s, w, a in sent
        ]
    )

    result = {
        **summary,
        "sent": len(sent),
        "confirmed": len(confirmed_sigs),
        "actual_sent": actual_sent,
        "leftover_actual": pool - actual_sent,
    }
    print(
        f"[distribute] {asset}: pool={pool} recipients={len(payouts)} sent={len(sent)} "
        f"confirmed={len(confirmed_sigs)} actual_sent={actual_sent} leftover={pool - actual_sent}"
    )
    return result


def distribute_sol(pool_lamports: int, weights: dict[str, int]) -> dict:
    """Direct SOL airdrop: split `pool_lamports` across `weights` and send
    System transfers. The pool argument comes from the sol_pool ACCUMULATOR
    (fed only by measured claim deltas) and is already bounded by the caller
    (cap + wallet-floor checks in cycle.py) — this function never reads the
    wallet balance and can never pay more than `pool_lamports`.

    Payouts below MIN_SOL_PAYOUT_LAMPORTS are skipped (each transfer burns ~5k
    lamports in fees; dust costs more to send than it delivers). Skipped dust
    is reported in leftover and stays in the pool for the next window.

    Returns a summary with `actual_sent` (confirmed lamports) — the caller
    decrements the accumulator by exactly this amount.
    """
    payouts, leftover = compute_payouts(pool_lamports, weights)
    floor = config.MIN_SOL_PAYOUT_LAMPORTS
    skipped_dust = sum(a for _, a in payouts if a < floor)
    payouts = [(w, a) for w, a in payouts if a >= floor]
    summary = {
        "ts": int(time.time()),
        "asset": "SOL",
        "pool": pool_lamports,
        "total_weight": sum(weights.values()),
        "recipients": len(payouts),
        "leftover_planned": leftover + skipped_dust,
    }

    if not payouts:
        print(f"[distribute] SOL: nothing to send (pool={pool_lamports}, all below dust floor)")
        return {**summary, "sent": 0, "actual_sent": 0, "confirmed": 0}

    if config.DRY_RUN:
        preview = payouts[:10]
        print(f"[distribute] DRY_RUN SOL: pool={pool_lamports} → {len(payouts)} recipients")
        for w, a in preview:
            print(f"[distribute]   DRY_RUN {w} <= {a} lamports")
        if len(payouts) > len(preview):
            print(f"[distribute]   … +{len(payouts) - len(preview)} more")
        return {**summary, "sent": 0, "actual_sent": 0, "confirmed": 0, "dry_run": True}

    chunk = max(1, config.DISTRIBUTE_CHUNK_SIZE)
    workers = max(1, config.DISTRIBUTE_CONCURRENCY)
    sent: list[tuple[str, str, int]] = []

    for start in range(0, len(payouts), chunk):
        group = payouts[start : start + chunk]
        try:
            blockhash = rpc.get_recent_blockhash()  # fail-CLOSED per chunk
        except RPCError as exc:
            print(f"[distribute] SOL blockhash fetch failed, stopping early: {exc}")
            break

        def _do(item: tuple[str, int]) -> Optional[tuple[str, str, int]]:
            wallet, amount = item
            try:
                ixs = stx.priority_fee_ixs() + [
                    stx.ix_transfer_sol(
                        from_pubkey=config.WALLET_PUBKEY,
                        to_pubkey=Pubkey.from_string(wallet),
                        lamports=amount,
                    )
                ]
                b64 = stx.build_tx_b64(ixs, blockhash)
            except ValueError as exc:
                print(f"[distribute] bad recipient {wallet}: {exc}")
                return None
            sig = _send_with_backoff(b64, label=f"SOL→{wallet}")
            return (sig, wallet, amount) if sig else None

        with ThreadPoolExecutor(max_workers=workers) as pool_exec:
            for res in pool_exec.map(_do, group):
                if res:
                    sent.append(res)

    confirmed_sigs = _confirm([s for s, _, _ in sent])
    actual_sent = sum(a for s, _, a in sent if s in confirmed_sigs)

    now = int(time.time())
    _append_payouts_log(
        [
            {"ts": now, "asset": "SOL", "wallet": w, "amount": a, "sig": s, "confirmed": s in confirmed_sigs}
            for s, w, a in sent
        ]
    )

    result = {
        **summary,
        "sent": len(sent),
        "confirmed": len(confirmed_sigs),
        "actual_sent": actual_sent,
    }
    print(
        f"[distribute] SOL: pool={pool_lamports} recipients={len(payouts)} sent={len(sent)} "
        f"confirmed={len(confirmed_sigs)} actual_sent={actual_sent}"
    )
    return result
