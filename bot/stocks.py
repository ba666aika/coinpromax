"""Stocks airdrop leg 1: buy the xStocks basket with SOL via Jupiter.

Budget is split evenly across config.STOCK_MINTS (top-5 xStocks). Each leg:
GET quote → POST swap (returns an UNSIGNED tx) → sign locally with the bot
keypair → submit through our own RPC. The key never leaves this process —
exactly the PumpPortal buyback pattern, with Jupiter lite-api (keyless, the
same service bankcoin uses for USDC↔$BANK).

SAFETY MODEL:
  * The TOTAL basket spend is capped by MAX_STOCK_BASKET_LAMPORTS in cycle.py
    BEFORE this module is called; `buy_basket` additionally re-caps as a
    belt-and-suspenders (this module must never exceed what it is given).
  * Each leg is self-isolating: a failed quote/swap just leaves that leg's SOL
    in the wallet for the next cycle. No retries inside a tick.
  * What was actually BOUGHT is never read from the quote — distribution reads
    the live on-wallet balance of each stock mint (delta-free, self-healing),
    exactly like the $CPM buyback.
  * DRY_RUN: log the planned legs, sign/send nothing.
"""
from __future__ import annotations

import base64

import httpx
from solders.transaction import VersionedTransaction

from . import config, rpc
from .rpc import RPCError


def _quote(input_mint: str, output_mint: str, lamports: int, timeout: float = 15.0) -> dict:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": lamports,
        "slippageBps": config.STOCK_SLIPPAGE_BPS,
    }
    try:
        r = httpx.get(config.JUPITER_QUOTE_URL, params=params, timeout=httpx.Timeout(timeout, connect=5.0))
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RPCError(f"jupiter quote failed: {exc}") from exc


def _swap_tx(quote: dict, timeout: float = 20.0) -> bytes:
    body = {
        "quoteResponse": quote,
        "userPublicKey": str(config.WALLET_PUBKEY),
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": int(config.PRIORITY_FEE_SOL * 1_000_000_000),
    }
    try:
        r = httpx.post(config.JUPITER_SWAP_URL, json=body, timeout=httpx.Timeout(timeout, connect=5.0))
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RPCError(f"jupiter swap build failed: {exc}") from exc
    tx_b64 = data.get("swapTransaction")
    if not tx_b64:
        raise RPCError(f"jupiter swap returned no tx: {str(data)[:200]}")
    return base64.b64decode(tx_b64)


def _sign_and_send(tx_bytes: bytes, *, label: str) -> str:
    unsigned = VersionedTransaction.from_bytes(tx_bytes)
    signed = VersionedTransaction(unsigned.message, [config.WALLET_KEYPAIR])
    b64 = base64.b64encode(bytes(signed)).decode("ascii")
    sig = rpc.send_raw_tx(b64)
    print(f"[stocks] sent {label}: {sig}")
    return sig


def buy_basket(lamports: int) -> list[str]:
    """Spend up to `lamports` of SOL on the stock basket, split evenly across
    config.STOCK_MINTS. Returns the signatures of the legs that were submitted.
    Never raises money-critical errors: a failed leg leaves its SOL in the
    wallet for the next cycle.
    """
    lamports = min(lamports, config.MAX_STOCK_BASKET_LAMPORTS)  # re-cap (defense in depth)
    if lamports <= 0:
        return []
    per_leg = lamports // len(config.STOCK_MINTS)
    if per_leg <= 0:
        return []

    if config.DRY_RUN:
        for mint in config.STOCK_MINTS:
            print(f"[stocks] DRY_RUN: would buy {per_leg} lamports ({per_leg/1e9:.6f} SOL) of {mint}")
        return []

    sol = str(config.WSOL_MINT)
    sigs: list[str] = []
    for mint in config.STOCK_MINTS:
        label = f"stock_buy({str(mint)[:8]}…,{per_leg})"
        try:
            quote = _quote(sol, str(mint), per_leg)
            tx_bytes = _swap_tx(quote)
            sigs.append(_sign_and_send(tx_bytes, label=label))
        except RPCError as exc:
            print(f"[stocks] {label} failed (SOL stays in wallet): {exc}")
            continue
    return sigs
