"""Task "callout" source — who made a CALLOUT on the coin's pump.fun page.

Callouts are a native pump.fun feature with a PUBLIC, auth-free API (verified
live against the production site — it is the exact request the coin page makes
for its callout tab):

    GET {base}/callout/top/{mint}?limit=N&sortBy=TIMESTAMP&sortOrder=DESC
    → {"callouts": [{"userId": "<wallet pubkey>", "coinMint": "...",
                      "createdAt": ms, "username": "...", ...}, ...]}

`userId` IS the author's wallet address — no JWT, no login, no signature.

Two sources, merged (mirrors bot.community):

  * the public callout feed for our mint               (auto, best-effort)
  * a manual allowlist file on the data volume         (operator override)

SAFETY MODEL:
  * `fetch_callout_wallets` RAISES on any network/HTTP/parse failure. The caller
    (cycle.py) catches it and proceeds fail-SAFE: existing sticky task flags are
    kept and this refresh is skipped. A flaky API must NEVER revoke a task.
  * We never fabricate a callout; a wallet counts only if it appears in the feed
    (or the allowlist). Only entries whose `coinMint` matches OUR mint count.
  * Endpoint path is env-overridable (PUMPFUN_CALLOUT_PATH) in case pump.fun
    reshuffles its frontend API again.
"""
from __future__ import annotations

import os

import httpx

from . import config

# Reuse the battle-tested allowlist reader (JSON list / {"wallets": []} /
# newline list, never raises).
from .community import load_allowlist as _load_allowlist_file


class PumpThreadError(RuntimeError):
    """Raised on any failure to read the callout feed (caller fails SAFE)."""


_CALLOUT_PATH = "/callout/top/{mint}"
_LIMIT = 1000   # server clamps as it likes; callout counts stay small anyway


def _looks_like_pubkey(s: str) -> bool:
    return 32 <= len(s) <= 44 and all(c not in "0OIl" and c.isalnum() for c in s)


def fetch_callout_wallets(mint: str, *, timeout: float = 12.0) -> set[str]:
    """Return the set of wallet addresses that made a callout on `mint`'s
    pump.fun page. Raises PumpThreadError on any failure so the caller can
    fail SAFE.
    """
    base = config.PUMPFUN_API_BASE.rstrip("/")
    path_tpl = os.environ.get("PUMPFUN_CALLOUT_PATH") or _CALLOUT_PATH
    url = base + path_tpl.replace("{mint}", mint)
    params = {"limit": _LIMIT, "sortBy": "TIMESTAMP", "sortOrder": "DESC"}
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

    try:
        r = httpx.get(url, params=params, headers=headers, timeout=httpx.Timeout(timeout, connect=5.0))
    except httpx.HTTPError as exc:
        raise PumpThreadError(f"callout request failed: {exc}") from exc
    if r.status_code != 200:
        raise PumpThreadError(f"callout HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as exc:
        raise PumpThreadError(f"callout response not JSON: {exc}") from exc

    items = data.get("callouts") if isinstance(data, dict) else data
    if items is None:
        raise PumpThreadError(f"callout response missing 'callouts': {str(data)[:120]}")

    out: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("coinMint") not in (None, mint):  # scope to OUR mint
            continue
        wallet = (item.get("userId") or "").strip()
        if wallet and _looks_like_pubkey(wallet):
            out.add(wallet)
    return out


def load_allowlist(path: str | None = None) -> set[str]:
    """Manual callout allowlist (operator-controlled fallback). Never raises."""
    return _load_allowlist_file(path or config.CALLOUT_ALLOWLIST_PATH)
