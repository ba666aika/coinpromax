"""Per-second held_seconds tracker — the heart of Coin Pro Max.

Invariant: any decrease in a wallet's $CPM balance between snapshots resets
held_seconds to 0 — "if you sell, the accumulated effect starts over". Buy-add
(increase) does NOT reset — time continues accruing from first_seen_ts.

Task flags (`tasks`) are STICKY and survive the reset: a pump.fun call-out or a
community bullpost is a fact that persists on those platforms, and the detectors
re-mark them from the live feeds anyway. The PUNISHMENT for selling is the
weight reset — weight = held_seconds × balance — which zeroes every airdrop
share until time re-accrues.

State file: /data/loyalty_state.json
{
  "<wallet_pubkey>": {
    "first_seen_ts":  int (unix seconds, when balance first observed > 0),
    "last_balance":   int (raw units, last successfully-read amount),
    "last_check_ts":  int (unix seconds, when last_balance was recorded),
    "held_seconds":   int (cumulative seconds held — reset on balance drop),
    "tasks":          {"callout": ts, "bullpost": ts}  (sticky, optional)
  },
  ...
}

Reads/writes are atomic via tmp-file + rename. Schema is forward-compatible;
unknown fields are preserved verbatim.
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterable

from . import config


def _now() -> int:
    return int(time.time())


def load_state() -> dict[str, dict]:
    """Read the on-disk state. Returns {} if missing (first run).

    On JSON-parse failure (corrupted file) we abort the bot — DO NOT
    silently start from scratch (that would zero everyone's held_seconds,
    which is morally equivalent to the Jobcoin fail-open drain pattern
    applied to loyalty instead of balances).
    """
    path = config.LOYALTY_STATE_PATH
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        # Let JSONDecodeError propagate — caller (cycle.py) must NOT
        # catch this and continue with empty state.
        return json.load(f)


def save_state(state: dict[str, dict]) -> None:
    """Atomic write: tmp + os.replace. Survives crashes mid-write."""
    path = config.LOYALTY_STATE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def update(
    state: dict[str, dict],
    live_balances: dict[str, int],
    *,
    now: int | None = None,
) -> dict[str, dict]:
    """Apply a balance snapshot to the state. Returns the same dict, mutated.

    - For each wallet in `live_balances`:
      - If unknown → seed: first_seen_ts=now, last_balance=amount, last_check_ts=now, held_seconds=0
      - If known and `amount < last_balance` (or `amount < MIN_HOLDING_RAW`) → RESET (held_seconds=0, first_seen_ts=now)
      - If known, not a decrease, but `last_balance < MIN_HOLDING_RAW` (was below the
        floor / out) → fresh ENTRY: held_seconds=0, first_seen_ts=now. No credit for
        the gap spent below the minimum, so a re-entrant gets no free head start over
        a brand-new holder.
      - If known, not a decrease, and already at/above the floor → accrue (held_seconds += now - last_check_ts)
      - last_balance updated to current amount in all cases
    - Wallets present in state but absent from `live_balances` are treated as
      `amount = 0` → reset (they sold completely or transferred everything).
      We KEEP them in state so they can re-enter cleanly later.

    `live_balances` MUST be a complete snapshot (every eligible wallet).
    Passing a partial snapshot would falsely reset everyone missing — the
    caller in cycle.py is responsible for ensuring completeness, and on
    RPCError the entire cycle is skipped instead of calling update() with
    partial data.
    """
    t = now if now is not None else _now()

    # Pass 1: known wallets — accrue or reset.
    seen: set[str] = set()
    for wallet, info in state.items():
        amount = live_balances.get(wallet, 0)
        seen.add(wallet)

        last_balance = int(info.get("last_balance", 0))
        last_check_ts = int(info.get("last_check_ts", t))

        if amount < last_balance or amount < config.MIN_HOLDING_RAW:
            # Any decrease → reset: the accumulated effect starts over (CPM
            # base rule). Below-min also counts as "exited". No permanent
            # flag — a seller may rebuy and rebuild from zero.
            info["held_seconds"] = 0
            info["first_seen_ts"] = t
            info["last_balance"] = amount
            info["last_check_ts"] = t
        elif last_balance < config.MIN_HOLDING_RAW:
            # Was below the floor at the last check (out / ineligible) and has now
            # crossed back above it without decreasing. This is a fresh ENTRY, not
            # a continuation: start the clock at zero, exactly like a brand-new
            # wallet. Accruing `now - last_check_ts` here (the naive branch below)
            # would award loyalty time for the gap the wallet spent BELOW the
            # minimum — time it was not eligible — and hand re-entrants a free head
            # start over genuinely new holders.
            info["held_seconds"] = 0
            info["first_seen_ts"] = t
            info["last_balance"] = amount
            info["last_check_ts"] = t
        else:
            # Was eligible last check and still is (same or increased) — accrue.
            delta = max(0, t - last_check_ts)
            info["held_seconds"] = int(info.get("held_seconds", 0)) + delta
            info["last_balance"] = amount
            info["last_check_ts"] = t

    # Pass 2: brand-new wallets.
    for wallet, amount in live_balances.items():
        if wallet in seen:
            continue
        if amount < config.MIN_HOLDING_RAW:
            continue
        state[wallet] = {
            "first_seen_ts": t,
            "last_balance": int(amount),
            "last_check_ts": t,
            "held_seconds": 0,
        }

    return state


def eligible_weights(state: dict[str, dict]) -> dict[str, int]:
    """Returns {wallet: weight} for wallets currently eligible.

    Weight = held_seconds. Wallets with held_seconds == 0 (fresh / just reset)
    get NO airdrop this cycle — they need at least one tick of held time first.
    This also naturally excludes wallets that just sold (reset to 0).

    AMM pools / vault PDAs / operator wallet / bot wallet are excluded via
    `EXCLUDED_OWNERS` at the cycle level, not here.
    """
    out: dict[str, int] = {}
    for wallet, info in state.items():
        held = int(info.get("held_seconds", 0))
        bal = int(info.get("last_balance", 0))
        if held <= 0:
            continue
        if bal < config.MIN_HOLDING_RAW:
            continue
        out[wallet] = held
    return out


def weighted_holdings(state: dict[str, dict]) -> dict[str, int]:
    """Payout weight = held_seconds × balance — your share depends on BOTH how
    long AND how much you hold. Same eligibility as eligible_weights
    (held_seconds > 0 and balance at/above the floor). Selling resets
    held_seconds to 0 → weight 0, so a seller earns nothing until it rebuys and
    re-accrues time. This is the weight for EVERY CPM airdrop (SOL / supply /
    stocks); the per-task airdrops additionally filter by task_weights().
    """
    out: dict[str, int] = {}
    for wallet, info in state.items():
        held = int(info.get("held_seconds", 0))
        bal = int(info.get("last_balance", 0))
        if held <= 0 or bal < config.MIN_HOLDING_RAW:
            continue
        out[wallet] = held * bal
    return out


def apply_task(state: dict[str, dict], wallets: Iterable[str], task: str, *, now: int | None = None) -> dict[str, dict]:
    """Sticky-mark `task` ("callout" / "bullpost") done for each wallet. NEVER
    clears a flag — a transient empty/failed fetch must not revoke anyone
    (fail-SAFE). Wallets not yet in state (did the task before they hold) are
    ignored now and picked up on a later refresh once they appear as holders.
    """
    t = now if now is not None else _now()
    for wallet in wallets:
        info = state.get(wallet)
        if info is not None:
            tasks = info.setdefault("tasks", {})
            if task not in tasks:
                tasks[task] = t
    return state


def task_weights(state: dict[str, dict], task: str) -> dict[str, int]:
    """weighted_holdings() narrowed to wallets that completed `task`. This is
    the weight set for the task-gated airdrops (supply ← callout, stocks ←
    bullpost)."""
    return {
        w: v
        for w, v in weighted_holdings(state).items()
        if task in (state.get(w, {}).get("tasks") or {})
    }


def stamp_lvl3(state: dict[str, dict], *, now: int | None = None) -> dict[str, dict]:
    """Track WHEN each wallet reached lvl 3 (eligible holder + callout +
    bullpost). `lvl3_since` is set on the first tick all three hold and CLEARED
    the moment the wallet drops out (sell / below floor) — so the lvl-4 waiting
    clock restarts from zero on re-entry. Must run every tick, AFTER the task
    flags are applied and BEFORE the state is persisted."""
    t = now if now is not None else _now()
    for info in state.values():
        held = int(info.get("held_seconds", 0))
        bal = int(info.get("last_balance", 0))
        tasks = info.get("tasks") or {}
        is_lvl3 = held > 0 and bal >= config.MIN_HOLDING_RAW and "callout" in tasks and "bullpost" in tasks
        if is_lvl3:
            info.setdefault("lvl3_since", t)
        else:
            info.pop("lvl3_since", None)
    return state


def casino_weights(state: dict[str, dict], *, now: int | None = None) -> dict[str, int]:
    """Lvl-4 (casino) candidates: wallets that completed ALL previous levels
    AND have held lvl 3 continuously for ≥ CASINO_MIN_LVL3_SECONDS (10 min =
    two casino cycles by default). The draw itself is UNIFORM (one ticket per
    wallet); the held×balance values are returned only for consistency with
    the other weight sets (stats etc.)."""
    t = now if now is not None else _now()
    out: dict[str, int] = {}
    for w, v in weighted_holdings(state).items():
        info = state.get(w, {})
        tasks = info.get("tasks") or {}
        if "callout" not in tasks or "bullpost" not in tasks:
            continue
        since = info.get("lvl3_since")
        if since is None or t - int(since) < config.CASINO_MIN_LVL3_SECONDS:
            continue  # not yet matured into lvl 4
        out[w] = v
    return out


def level_of(info: dict) -> int:
    """CPM level of one tracked wallet. Level 1 = an eligible holder with at
    least a tick of held time (buy & hold, no sells); +1 per completed task.
    A wallet that just reset (or is below the floor) is level 0 — it keeps its
    sticky task flags but must rebuild held time to level up again.
    """
    held = int(info.get("held_seconds", 0))
    bal = int(info.get("last_balance", 0))
    if held <= 0 or bal < config.MIN_HOLDING_RAW:
        return 0
    return 1 + len(info.get("tasks") or {})


def filter_excluded(
    weights: dict[str, int],
    excluded_owners: Iterable[str],
) -> dict[str, int]:
    excluded = set(excluded_owners)
    return {w: v for w, v in weights.items() if w not in excluded}
