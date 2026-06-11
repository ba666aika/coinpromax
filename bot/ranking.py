"""Rank-based loyalty weighting — the shared, dependency-free core.

Loyalty is RELATIVE: a wallet's reward share depends only on its RANK (its
position when all eligible wallets are sorted by held_seconds, longest first),
never on the absolute held_seconds. The longest holder is always #1 and gets
the biggest share even if it leads by a single second.

This lives apart from distribute.py on purpose: the read-only web server
(server.py) imports the EXACT same curve to display real shares, and must NOT
pull in bot.config (which loads the wallet key). Pure stdlib (math only), no
I/O, no config — so `from bot.ranking import ...` is safe in the key-less web
process. The engine (distribute.py) imports the same `rank_weights`, so the
number shown on the bubble map is the same number that gets paid — no drift.
"""
from __future__ import annotations

import math


def rank_weights(held: dict[str, int], *, scale: int = 1_000_000_000) -> dict[str, int]:
    """Convert held_seconds → rank-based payout weights via a 1/sqrt(rank) curve.

        weight(rank) = scale / sqrt(rank)

    Share depends ONLY on RANK (longest held = rank 1), not on absolute seconds:
    a 1-second lead gives the same advantage as a huge lead. `scale` only sets
    integer precision (compute_payouts needs int weights); it cancels in the
    pool * w // total_w ratio, so its exact value is irrelevant.

    Ties (identical held_seconds) are resolved FAIRLY: wallets sharing a value
    occupy a contiguous block of ranks and each receives the AVERAGE weight over
    that block. So equal held time always yields equal payout, and tying can
    neither inflate nor deflate the group's total share (the curve shape is
    preserved). Pubkey string is the deterministic tiebreak for ordering only.

    Only wallets with held_seconds > 0 are kept. Input is expected to already be
    the eligible, excluded-filtered set. Returns {} for no input.
    """
    items = [(w, int(h)) for w, h in held.items() if int(h) > 0]
    if not items:
        return {}
    items.sort(key=lambda kv: (-kv[1], kv[0]))  # held desc, pubkey asc for determinism
    out: dict[str, int] = {}
    i = 0
    n = len(items)
    pos = 1  # 1-based rank of items[i]
    while i < n:
        j = i
        while j < n and items[j][1] == items[i][1]:
            j += 1
        count = j - i  # size of this tie block, occupying ranks pos .. pos+count-1
        avg = sum(1.0 / math.sqrt(p) for p in range(pos, pos + count)) / count
        wt = max(1, round(scale * avg))
        for k in range(i, j):
            out[items[k][0]] = wt
        pos += count
        i = j
    return out


def ranked_holders(held: dict[str, int]) -> list[dict]:
    """Order holders best-first with their rank and rank-curve reward share.

    Input: {wallet: held_seconds} (already eligible / excluded-filtered).
    Output: list of {wallet, held_seconds, rank, share_bps} sorted by
    held_seconds desc (pubkey tiebreak), where:
      - `rank` is 1-based; tied wallets (equal held_seconds) share a rank number
        (competition ranking: 1, 2, 2, 4, ...),
      - `share_bps` is the wallet's share of the reward pool in basis points
        under the 1/sqrt(rank) curve (floor; the sum is ≤ 10000, the small
        remainder is the un-allocated dust, exactly like the on-chain payout).

    This is the single source of truth for "who gets what" shown on the site;
    it mirrors what cycle.py feeds into the airdrop.
    """
    weights = rank_weights(held)
    total = sum(weights.values())
    items = sorted(
        ((w, int(h)) for w, h in held.items() if int(h) > 0),
        key=lambda kv: (-kv[1], kv[0]),
    )
    out: list[dict] = []
    rank = 0
    prev_held = None
    for pos, (wallet, h) in enumerate(items, start=1):
        if h != prev_held:
            rank = pos  # competition ranking: ties share a rank, next jumps
            prev_held = h
        share_bps = int(weights.get(wallet, 0) * 10_000 / total) if total else 0
        out.append(
            {"wallet": wallet, "held_seconds": h, "rank": rank, "share_bps": share_bps}
        )
    return out
