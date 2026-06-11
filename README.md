# Coin Pro Max

"It has everything and even more." Levels + tasks + multi-asset rewards on the
shared claim→split→distribute engine (USUG/sharecat lineage, all guards in).

## Levels

Base rule: **hold without selling**. ANY sell → accumulated time resets to 0
(restart allowed, no permanent flag). Every airdrop is weighted by
`held_seconds × balance`.

| Lvl | Task | Reward unlocked | Detected via |
|---|---|---|---|
| 1 | buy & hold (no sells) | SOL airdrop | holder snapshots (every tick) |
| 2 | call-out in the coin's pump.fun thread | $CPM supply airdrop (buyback → distribute) | pump.fun replies API (JWT via message signature) + `/data/callout_allowlist.json` |
| 3 | bullpost in the coin's community | xStocks basket airdrop | coincommunities server API + `/data/engaged_allowlist.json` |

Levels are cumulative (lvl 3 receives all three). Task flags are sticky; the
sell-punishment is the weight reset.

## Fee split (per claim, `claimed` = confirmed wallet delta)

```
20% → OPERATOR_WALLET (dev cut)
30% → ad-bounty reserve — STAYS on the bot wallet, bot never spends it
50% → reward pool, split evenly:
      ├─ SOL airdrop      (accumulates in /data/sol_pool.json, paid per window)
      ├─ $CPM buyback     (PumpPortal, ≤ MAX_BUYBACK_LAMPORTS)
      └─ xStocks basket   (Jupiter, 5 mints even split, ≤ MAX_STOCK_BASKET_LAMPORTS)
```

Stocks basket default (verified live routes, override via `STOCK_MINTS`):
CRCLx, SPYx, QQQx, TSLAx, NVDAx — top-5 genuine xStocks by Jupiter 24h volume.

**Ad reserve bookkeeping:** the bot only increments `/data/ad_reserve.json`.
When you withdraw/spend ad money from the wallet, decrement that file by hand —
the SOL airdrop treats it as untouchable and pays only
`min(sol_pool, MAX_SOL_AIRDROP_LAMPORTS, balance − GAS_FLOOR − ad_reserve)`.

## Processes

- `start.py` — launcher: key-less web (socket listener, secrets stripped from
  env) + supervised bot (no inbound listener). Boot-time state wipe on
  `WIPE_DATA=1` or mint change.
- `server.py` — GET-only API: `/api/stats`, `/api/wallet?wallet=…` (level,
  tasks, share — the "check wallet" bar), `/api/board`, static `frontend/`.
- `python -m bot` — the money loop.

## Env (Railway Variables)

Required: `WALLET_PRIVATE_KEY` (NEVER anywhere else), `HELIUS_API_KEY`,
`LOYALTY_MINT` (the $CPM mint), `OPERATOR_WALLET`.
Tasks: `COINCOMMUNITIES_API_KEY` / `COINCOMMUNITIES_API_SECRET` (secrets).
Optional knobs: `OPERATOR_PCT` / `AD_RESERVE_PCT` / `REWARD_PCT` (sum 1.0),
`MAX_BUYBACK_LAMPORTS`, `MAX_STOCK_BASKET_LAMPORTS`, `MAX_SOL_AIRDROP_LAMPORTS`,
`GAS_FLOOR_LAMPORTS`, `MIN_SOL_PAYOUT_LAMPORTS`, `MIN_HOLDING_RAW`,
`STOCK_MINTS`, `TASKS_REFRESH_SECONDS`, `DRY_RUN`.

## Pre-launch checklist

1. Fresh wallet; mint $CPM FROM that wallet (creator = bot wallet).
2. `DRY_RUN=1` first pass against live Helius; watch one full cycle.
3. **Verify the pump.fun replies endpoint live** (`PUMPFUN_API_BASE`,
   `PUMPFUN_REPLIES_PATH`, login message template `PUMPFUN_LOGIN_MESSAGE`) —
   pump.fun reshuffles its frontend API; the callout allowlist file is the
   fallback if the feed is down at launch.
4. Railway volume on `/data`; no env edits mid-deploy.

## Tests

```
python3 -m pytest tests/   # 116 tests: split math, caps, fail-closed, tasks,
                           # SOL-pool bounds, basket isolation, 10k stress
```
