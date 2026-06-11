"""Environment-driven config. Values validated at import time —
the bot refuses to boot with a missing/malformed critical value
(better than running with silent defaults that send money sideways).
"""
from __future__ import annotations

import os
import sys

from solders.keypair import Keypair
from solders.pubkey import Pubkey


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"[config] FATAL: missing required env {name}", file=sys.stderr)
        sys.exit(2)
    return v


def _opt(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        print(f"[config] FATAL: env {name} is not an int", file=sys.stderr)
        sys.exit(2)


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        print(f"[config] FATAL: env {name} is not a float", file=sys.stderr)
        sys.exit(2)


# === RPC ===
HELIUS_API_KEY = _require("HELIUS_API_KEY")
RPC_URL = _opt(
    "RPC_URL",
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
)

# === Wallet (bot signer) ===
_PRIVKEY_B58 = _require("WALLET_PRIVATE_KEY")
try:
    WALLET_KEYPAIR = Keypair.from_base58_string(_PRIVKEY_B58)
except Exception as exc:
    print(f"[config] FATAL: WALLET_PRIVATE_KEY is not valid base58 64-byte key: {exc}", file=sys.stderr)
    sys.exit(2)
WALLET_PUBKEY: Pubkey = WALLET_KEYPAIR.pubkey()
# Wipe the env var so it doesn't accidentally surface in logs / crash dumps.
os.environ.pop("WALLET_PRIVATE_KEY", None)
_PRIVKEY_B58 = None  # type: ignore[assignment]

# === Token + operator ===
try:
    LOYALTY_MINT: Pubkey = Pubkey.from_string(_require("LOYALTY_MINT"))
except Exception as exc:
    print(f"[config] FATAL: LOYALTY_MINT is not a valid pubkey: {exc}", file=sys.stderr)
    sys.exit(2)

try:
    OPERATOR_WALLET: Pubkey = Pubkey.from_string(_require("OPERATOR_WALLET"))
except Exception as exc:
    print(f"[config] FATAL: OPERATOR_WALLET is not a valid pubkey: {exc}", file=sys.stderr)
    sys.exit(2)

# === Distribution math (Coin Pro Max split) ===
# 20% SOL → operator (dev cut, separate wallet);
# 20% SOL → ad-bounty reserve: STAYS on the bot wallet, the bot never spends
#           it (the operator's manual ad budget) — tracked for stats only;
# 15% SOL → lvl 1: SOL airdrop pool;
# 15% SOL → lvl 2: $CPM buyback → supply airdrop;
# 15% SOL → lvl 3: xStocks basket → stocks airdrop;
# 15% SOL → lvl 4: CASINO — every CASINO_INTERVAL one UNIFORM-random wallet
#           that completed ALL previous levels wins the pool.
OPERATOR_PCT = _float("OPERATOR_PCT", 0.20)
AD_RESERVE_PCT = _float("AD_RESERVE_PCT", 0.20)
SOL_AIRDROP_PCT = _float("SOL_AIRDROP_PCT", 0.15)
SUPPLY_PCT = _float("SUPPLY_PCT", 0.15)
STOCKS_PCT = _float("STOCKS_PCT", 0.15)
CASINO_PCT = _float("CASINO_PCT", 0.15)
_total_pct = OPERATOR_PCT + AD_RESERVE_PCT + SOL_AIRDROP_PCT + SUPPLY_PCT + STOCKS_PCT + CASINO_PCT
if abs(_total_pct - 1.0) > 1e-6:
    print(
        "[config] FATAL: OPERATOR + AD_RESERVE + SOL_AIRDROP + SUPPLY + STOCKS + CASINO "
        f"percentages must sum to 1.0 (got {_total_pct})",
        file=sys.stderr,
    )
    sys.exit(2)

# Hard cap on a single buyback. SECURITY: regardless of claimed amount,
# a single buyback can never exceed this. Mandatory for SOL coins.
MAX_BUYBACK_LAMPORTS = _int("MAX_BUYBACK_LAMPORTS", 5_000_000_000)  # 5 SOL default

# Hard cap on ONE stock-basket purchase (the whole basket, all 5 legs summed).
# Same last-line-of-defense spirit as MAX_BUYBACK_LAMPORTS.
MAX_STOCK_BASKET_LAMPORTS = _int("MAX_STOCK_BASKET_LAMPORTS", 5_000_000_000)

# Hard cap on the SOL paid out by ONE sol-airdrop window. The pool itself is an
# accumulator (sol_pool.json) fed only by measured claim deltas, but the cap
# bounds the blast radius if that accounting ever goes wrong upstream.
MAX_SOL_AIRDROP_LAMPORTS = _int("MAX_SOL_AIRDROP_LAMPORTS", 5_000_000_000)

# Never let the SOL airdrop touch the bottom of the wallet: gas + the ad-bounty
# reserve live there too. The airdrop pays min(sol_pool, balance − floor − ad_reserve).
GAS_FLOOR_LAMPORTS = _int("GAS_FLOOR_LAMPORTS", 50_000_000)  # 0.05 SOL

# Skip SOL payouts below this (each transfer burns ~5k lamports in fees; paying
# dust costs more than it delivers). Skipped dust stays in the pool and rolls over.
MIN_SOL_PAYOUT_LAMPORTS = _int("MIN_SOL_PAYOUT_LAMPORTS", 100_000)

# Eligibility: a wallet must hold at least this many raw units of $LOYALTY to
# qualify (and to keep accruing held_seconds). Default = 50_000 tokens at the
# pump.fun-standard 6 decimals (50_000 * 10**6). Pump.fun mints are 6 decimals;
# if a future mint differs, override MIN_HOLDING_RAW in env to the raw amount.
MIN_HOLDING_RAW = _int("MIN_HOLDING_RAW", 50_000_000_000)

# DRY_RUN: build and log every action (claim/operator-cut/buyback/distribute)
# WITHOUT signing or submitting a single transaction. Used for the first live
# pass against Helius to verify the whole loop safely before moving real SOL.
# Default OFF (production moves money). The boot banner states the active mode.
DRY_RUN = (os.environ.get("DRY_RUN") or "0").strip().lower() in ("1", "true", "yes", "on")

# === Cadences ===
# The tick (= holder snapshot / held_seconds) runs every CYCLE_INTERVAL_SECONDS —
# kept short for fast sell-detection. Claim+cut+split and each airdrop have
# their OWN gate, so money moves less often than the snapshot.
#
# FEE MATH (why CPM defaults are slower than USUG's): one airdrop window costs
# up to 7 txs PER HOLDER (1 SOL + 1 $CPM + 5 stocks) × ~55k lamports each. At
# 100 holders a 5-minute window would burn ~11 SOL/day in fees alone. Hourly
# CPM/SOL windows + 6-hourly stocks windows cut that ~20×; nothing is lost —
# pools accumulate and roll into the next window.
CYCLE_INTERVAL_SECONDS = _int("CYCLE_INTERVAL_SECONDS", 10)      # snapshot holders every 10s
CLAIM_INTERVAL_SECONDS = _int("CLAIM_INTERVAL_SECONDS", 300)     # claim fees every 5 min (2 txs even when vault is empty)
AIRDROP_INTERVAL_SECONDS = _int("AIRDROP_INTERVAL_SECONDS", 3600)   # SOL + $CPM legs: hourly
# The stocks leg is the expensive one (5 txs per holder + ~0.002 SOL ATA rent
# per stock per NEW recipient) → its own, slower gate.
STOCKS_AIRDROP_INTERVAL_SECONDS = _int("STOCKS_AIRDROP_INTERVAL_SECONDS", 21600)  # 6h

# Don't fire a swap until its accumulated budget is worth the ~60k-lamport tx
# overhead. Shares below this keep accumulating in the buy-pools (never lost).
MIN_SWAP_LAMPORTS = _int("MIN_SWAP_LAMPORTS", 5_000_000)  # 0.005 SOL ≈ 1.2% overhead

# Dust floors for the SPL airdrops (mirror MIN_SOL_PAYOUT_LAMPORTS): paying a
# holder less than this costs more in fees than it delivers. Skipped dust stays
# in the live pool and rolls into the next window automatically.
MIN_CPM_PAYOUT_RAW = _int("MIN_CPM_PAYOUT_RAW", 1_000_000)   # 1 token @ 6 decimals
MIN_STOCK_PAYOUT_RAW = _int("MIN_STOCK_PAYOUT_RAW", 10_000)  # 1e-4 share @ 8 decimals

# Slippage on buyback (bps). 100 = 1%.
BUYBACK_SLIPPAGE_BPS = _int("BUYBACK_SLIPPAGE_BPS", 500)  # 5% — pump.fun is volatile

# Priority fee (in SOL) attached to bot-built txs and the PumpPortal buyback.
# Small flat tip so claims/transfers land during congestion.
PRIORITY_FEE_SOL = _float("PRIORITY_FEE_SOL", 0.00005)

# PumpPortal local (non-custodial) trade endpoint — returns an UNSIGNED tx that
# we sign locally with WALLET_KEYPAIR. The key never leaves this process.
PUMPPORTAL_TRADE_LOCAL_URL = _opt(
    "PUMPPORTAL_TRADE_LOCAL_URL", "https://pumpportal.fun/api/trade-local"
)

# === Distribution concurrency / RPC resilience ===
DISTRIBUTE_CONCURRENCY = _int("DISTRIBUTE_CONCURRENCY", 5)
DISTRIBUTE_CHUNK_SIZE = _int("DISTRIBUTE_CHUNK_SIZE", 150)  # one blockhash per chunk

# === State ===
DATA_DIR = _opt("DATA_DIR", "/data")
LOYALTY_STATE_PATH = f"{DATA_DIR}/loyalty_state.json"
STATS_PATH = f"{DATA_DIR}/stats.json"
PAYOUTS_PATH = f"{DATA_DIR}/payouts.jsonl"
# Accumulators (display + accounting). sol_pool is MONEY-CRITICAL: it is the
# only part of the wallet's SOL the airdrop may pay out. ad_reserve is stats-only
# (that SOL just sits on the wallet as the operator's manual ad budget).
# cpm_buy_pool / stock_buy_pool collect the per-claim swap budgets until they
# clear MIN_SWAP_LAMPORTS — so micro-claims aren't wasted on micro-swaps.
SOL_POOL_PATH = f"{DATA_DIR}/sol_pool.json"
AD_RESERVE_PATH = f"{DATA_DIR}/ad_reserve.json"
CPM_BUY_POOL_PATH = f"{DATA_DIR}/cpm_buy_pool.json"
STOCK_BUY_POOL_PATH = f"{DATA_DIR}/stock_buy_pool.json"
CASINO_POOL_PATH = f"{DATA_DIR}/casino_pool.json"
# Lifetime counter of SOL actually spent on holders (SOL airdrops + casino wins
# + buyback spends + stock-basket spends, all in lamports at spend time) — the
# "distributed all-time" number on the site.
TOTAL_DISTRIBUTED_PATH = f"{DATA_DIR}/total_distributed.json"

# === Casino (lvl 4) ===
# Every CASINO_INTERVAL_SECONDS one wallet that completed ALL previous levels
# (eligible holder + callout + bullpost) wins the casino pool. UNIFORM random:
# every eligible wallet is exactly one ticket regardless of size (operator's
# explicit choice). The eligibility bar itself is the anti-sybil cost — each
# wallet needs MIN_HOLDING plus its own pump.fun call-out and community
# bullpost. One winner = one tx per draw, so a 5-min cadence is cheap.
CASINO_INTERVAL_SECONDS = _int("CASINO_INTERVAL_SECONDS", 300)
# Lvl-4 entry bar: a wallet must have been CONTINUOUSLY at lvl 3 for at least
# this long (default 10 min = 2 casino cycles) before it enters the draws.
# Dropping out of lvl 3 (any sell / below floor) restarts the clock.
CASINO_MIN_LVL3_SECONDS = _int("CASINO_MIN_LVL3_SECONDS", 600)
# Don't draw until the pot is worth more than the tx overhead; below this the
# pool just keeps growing (never lost).
MIN_CASINO_DRAW_LAMPORTS = _int("MIN_CASINO_DRAW_LAMPORTS", 5_000_000)
# Hard cap on a single win (last line of defense). Excess stays pooled for
# the next draws.
MAX_CASINO_PAYOUT_LAMPORTS = _int("MAX_CASINO_PAYOUT_LAMPORTS", 5_000_000_000)

# === Tasks (Coin Pro Max levels) ===
# Task "bullpost" — posted in the coin's community on coincommunities.org.
# Server key + secret (cck_… / ccs_…) from admin.coincommunities.org → sent as the
# x-server-key / x-server-secret headers. BOTH are SECRETS — Railway Variables only.
COINCOMMUNITIES_API_BASE = _opt("COINCOMMUNITIES_API_BASE", "https://api.coin-communities.xyz")
COINCOMMUNITIES_API_KEY = os.environ.get("COINCOMMUNITIES_API_KEY") or ""
COINCOMMUNITIES_API_SECRET = os.environ.get("COINCOMMUNITIES_API_SECRET") or ""
# How often to re-pull task sets from the external APIs (seconds). The local
# allowlist files are read every tick regardless; only network calls throttle.
TASKS_REFRESH_SECONDS = _int("TASKS_REFRESH_SECONDS", 300)
# Manual fallback/override allowlists (JSON list / {"wallets": []} / newline list).
# ALWAYS merged in. Live on the volume so they survive redeploys.
ENGAGED_ALLOWLIST_PATH = _opt("ENGAGED_ALLOWLIST_PATH", f"{DATA_DIR}/engaged_allowlist.json")
CALLOUT_ALLOWLIST_PATH = _opt("CALLOUT_ALLOWLIST_PATH", f"{DATA_DIR}/callout_allowlist.json")

# Task "callout" — commented in the coin's thread on pump.fun. The replies API
# needs a JWT obtained by signing a login message with the bot key (an off-chain
# MESSAGE signature — it can never move funds). Endpoint shape is configurable
# because pump.fun reshuffles its frontend API between versions.
PUMPFUN_API_BASE = _opt("PUMPFUN_API_BASE", "https://frontend-api-v3.pump.fun")

# === Stocks basket (xStocks via Jupiter) ===
# Top-5 genuine xStocks (backed.fi) by Jupiter 24h volume, verified 2026-06-11 —
# every mint carries the `xstocks` tag on lite-api.jup.ag and has a live
# SOL→stock route. Override via STOCK_MINTS (comma-separated mints).
# CRCLx, SPYx, QQQx, TSLAx, NVDAx — all decimals=8, Token-2022.
_DEFAULT_STOCK_MINTS = (
    "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1,"  # CRCLx
    "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W,"  # SPYx
    "Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ,"  # QQQx
    "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB,"  # TSLAx
    "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh"   # NVDAx
)
STOCK_MINTS: list[Pubkey] = []
for _m in (os.environ.get("STOCK_MINTS") or _DEFAULT_STOCK_MINTS).replace(" ", "").split(","):
    if not _m:
        continue
    try:
        STOCK_MINTS.append(Pubkey.from_string(_m))
    except Exception as exc:
        print(f"[config] FATAL: STOCK_MINTS entry {_m!r} is not a valid pubkey: {exc}", file=sys.stderr)
        sys.exit(2)
if not STOCK_MINTS:
    print("[config] FATAL: STOCK_MINTS is empty", file=sys.stderr)
    sys.exit(2)

# Jupiter lite-api (keyless) — quote + swap, same service bankcoin uses.
JUPITER_QUOTE_URL = _opt("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
JUPITER_SWAP_URL = _opt("JUPITER_SWAP_URL", "https://lite-api.jup.ag/swap/v1/swap")
STOCK_SLIPPAGE_BPS = _int("STOCK_SLIPPAGE_BPS", 300)  # 3% — xStocks are liquid

# === Pump.fun program IDs (mainnet, from handoff-tech.md + official IDL) ===
PUMPFUN_BONDING_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMPSWAP_AMM_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")

# Wrapped SOL — the quote mint for pump.fun creator fees on the AMM side.
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

# === SPL Token-2022 (loyalty mint uses Token-2022 like $BANK) ===
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOC_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")

# AMM pools / vault PDAs that must never be paid out as if they were holders.
# Three layers: (1) the bot + operator wallets, (2) an explicit comma-separated
# override list (EXTRA_EXCLUDED_OWNERS) for instant additions without a redeploy,
# (3) runtime auto-detection in cycle.py of any holder whose on-chain account is
# program-owned (a pool / PDA / bonding curve), not a real System-Program wallet.
_EXTRA_EXCLUDED = {a for a in (os.environ.get("EXTRA_EXCLUDED_OWNERS") or "").replace(" ", "").split(",") if a}
EXCLUDED_OWNERS: set[str] = {
    str(WALLET_PUBKEY),  # never pay yourself
    str(OPERATOR_WALLET),
} | _EXTRA_EXCLUDED

# Auto-exclude program-owned holders (every AMM/LP pool, bonding curve, vault PDA)
# without enumerating program ids. On by default; set AUTODETECT_POOLS=0 to disable.
AUTODETECT_POOLS = (os.environ.get("AUTODETECT_POOLS") or "1").strip().lower() in ("1", "true", "yes", "on")
