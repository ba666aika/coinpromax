"""Buyback: spend SOL to buy $LOYALTY via PumpPortal's local (non-custodial)
trade API. PumpPortal returns an UNSIGNED transaction; we sign it locally with
the bot keypair and submit through our own RPC. The private key never leaves
this process.

The amount passed in is already capped by MAX_BUYBACK_LAMPORTS in cycle.py —
this module must never exceed what it is given. `bought` ($LOYALTY received) is
measured by the caller as the on-wallet balance delta; the quote's outAmount is
never trusted.
"""
from __future__ import annotations

import base64
from typing import Optional

import httpx
from solders.transaction import VersionedTransaction

from . import config, rpc
from .rpc import RPCError

# pump-amm is the pool a coin trades on after it graduates off the bonding
# curve; "auto" lets PumpPortal pick, with this as an explicit fallback.
_FALLBACK_POOL = "pump-amm"


def _request_unsigned_tx(lamports: int, pool: str) -> bytes:
    sol_amount = lamports / 1_000_000_000
    body = {
        "publicKey": str(config.WALLET_PUBKEY),
        "action": "buy",
        "mint": str(config.LOYALTY_MINT),
        "denominatedInSol": "true",
        "amount": sol_amount,
        "slippage": config.BUYBACK_SLIPPAGE_BPS / 100,  # PumpPortal wants percent
        "priorityFee": config.PRIORITY_FEE_SOL,
        "pool": pool,
    }
    try:
        r = httpx.post(config.PUMPPORTAL_TRADE_LOCAL_URL, json=body, timeout=httpx.Timeout(20.0, connect=5.0))
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise RPCError(f"pumpportal trade-local ({pool}) failed: {exc}") from exc
    content = r.content
    if not content or len(content) < 64:
        # PumpPortal returns a JSON error body (not tx bytes) on bad requests.
        raise RPCError(f"pumpportal trade-local ({pool}) returned non-tx body: {content[:200]!r}")
    return content


def _sign_and_send(tx_bytes: bytes, *, label: str) -> str:
    unsigned = VersionedTransaction.from_bytes(tx_bytes)
    signed = VersionedTransaction(unsigned.message, [config.WALLET_KEYPAIR])
    b64 = base64.b64encode(bytes(signed)).decode("ascii")
    sig = rpc.send_raw_tx(b64)
    print(f"[swap] sent {label}: {sig}")
    return sig


def buyback(lamports: int) -> Optional[str]:
    """Buy $LOYALTY with `lamports` of SOL. Returns the tx signature, or None
    under DRY_RUN / on a handled failure. Tries pool=auto, then pump-amm.

    Raises nothing money-critical: a failed buyback leaves the SOL in the wallet
    for the next cycle. The caller measures the actual $LOYALTY received as a
    balance delta, so an over/under-fill cannot corrupt distribution math.
    """
    if lamports <= 0:
        return None
    if config.DRY_RUN:
        print(f"[swap] DRY_RUN: would buy back with {lamports} lamports ({lamports/1e9:.6f} SOL)")
        return None

    # auto first (PumpPortal picks), then explicit bonding-curve ("pump") for a
    # coin still on the curve, then the post-migration AMM pool. Covers a coin at
    # any stage — not-yet-bonded OR graduated.
    for pool in ("auto", "pump", _FALLBACK_POOL):
        try:
            tx_bytes = _request_unsigned_tx(lamports, pool)
            return _sign_and_send(tx_bytes, label=f"buyback({pool})")
        except RPCError as exc:
            print(f"[swap] buyback via pool={pool} failed: {exc}")
            continue
    print("[swap] buyback failed on all pools — SOL stays in wallet for next cycle")
    return None
