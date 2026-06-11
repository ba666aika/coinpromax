"""Task "callout" source — which wallets commented in OUR coin's pump.fun thread.

pump.fun's replies API (GET {base}/replies/{mint}) requires a JWT. The JWT comes
from POST /auth/login with an ed25519 signature of a timestamped login message,
signed by the bot keypair. That is an OFF-CHAIN MESSAGE signature — it proves
key ownership to pump.fun and can never move funds (it is not a transaction and
is never submitted on-chain). The bot already holds this key to sign real txs,
so no new exposure is created.

Two sources, merged (mirrors bot.community):

  * the pump.fun replies feed for our mint            (auto, best-effort)
  * a manual allowlist file on the data volume        (reliable fallback/override)

SAFETY MODEL:
  * `fetch_callout_wallets` RAISES on any network/HTTP/parse failure. The caller
    (cycle.py) catches it and proceeds fail-SAFE: existing sticky task flags are
    kept and this refresh is skipped. A flaky API must NEVER revoke a task.
  * We never fabricate a callout; a wallet counts only if it authored a reply in
    OUR mint's thread (or is on the allowlist).
  * The endpoint paths/fields are env-overridable: pump.fun reshuffles its
    frontend API between versions, and the allowlist keeps the task usable even
    if the feed breaks entirely. VERIFY LIVE AT LAUNCH (the real thread exists
    only once the coin does).
"""
from __future__ import annotations

import base64
import json
import os
import time

import httpx

from . import config

# Reuse the battle-tested allowlist reader (JSON list / {"wallets": []} /
# newline list, never raises).
from .community import load_allowlist as _load_allowlist_file


class PumpThreadError(RuntimeError):
    """Raised on any failure to read the pump.fun thread (caller fails SAFE)."""


_LOGIN_PATH = "/auth/login"
_REPLIES_PATH = "/replies/{mint}"
# Candidate author fields — pump.fun has shipped several shapes over time.
_WALLET_FIELDS = ("user", "userAddress", "walletAddress", "wallet", "author")
_PAGE_LIMIT = 1000
_MAX_PAGES = 50           # hard cap so a misbehaving API can't loop forever
_TOKEN_TTL_S = 45 * 60    # re-login comfortably before typical 1h JWT expiry

_cached_token: str | None = None
_cached_token_ts: float = 0.0


def _login_message(address: str, timestamp_ms: int) -> bytes:
    tpl = os.environ.get("PUMPFUN_LOGIN_MESSAGE") or "Sign in to pump.fun: {timestamp}"
    return tpl.replace("{timestamp}", str(timestamp_ms)).encode()


def _login(timeout: float = 12.0) -> str:
    """POST /auth/login with a message signature from the bot key → JWT.
    Cached for _TOKEN_TTL_S. Raises PumpThreadError on any failure."""
    global _cached_token, _cached_token_ts
    if _cached_token and (time.time() - _cached_token_ts) < _TOKEN_TTL_S:
        return _cached_token

    address = str(config.WALLET_PUBKEY)
    ts_ms = int(time.time() * 1000)
    try:
        sig = config.WALLET_KEYPAIR.sign_message(_login_message(address, ts_ms))
        signature_b58 = str(sig)
    except Exception as exc:
        raise PumpThreadError(f"login message signing failed: {exc}") from exc

    url = config.PUMPFUN_API_BASE.rstrip("/") + _LOGIN_PATH
    body = {"address": address, "signature": signature_b58, "timestamp": ts_ms}
    try:
        r = httpx.post(url, json=body, timeout=httpx.Timeout(timeout, connect=5.0))
    except httpx.HTTPError as exc:
        raise PumpThreadError(f"login request failed: {exc}") from exc
    if r.status_code not in (200, 201):
        raise PumpThreadError(f"login HTTP {r.status_code}: {r.text[:200]}")

    token = None
    try:
        data = r.json()
        if isinstance(data, dict):
            token = data.get("access_token") or data.get("token") or data.get("jwt")
    except ValueError:
        pass
    if not token:
        # Some versions set the JWT as an auth_token cookie instead of a body field.
        token = r.cookies.get("auth_token")
    if not token:
        raise PumpThreadError(f"login returned no token (body: {r.text[:200]})")

    _cached_token = token
    _cached_token_ts = time.time()
    return token


def _wallet_of(msg: dict) -> str | None:
    for k in _WALLET_FIELDS:
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            inner = v.get("address") or v.get("walletAddress") or v.get("wallet")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def fetch_callout_wallets(mint: str, *, timeout: float = 12.0) -> set[str]:
    """Return the set of wallet addresses that commented in `mint`'s pump.fun
    thread. Raises PumpThreadError on any failure so the caller can fail SAFE.
    """
    token = _login(timeout=timeout)
    base = config.PUMPFUN_API_BASE.rstrip("/")
    path_tpl = os.environ.get("PUMPFUN_REPLIES_PATH") or _REPLIES_PATH
    url = base + path_tpl.replace("{mint}", mint)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    out: set[str] = set()
    offset = 0
    for _ in range(_MAX_PAGES):
        params = {"limit": _PAGE_LIMIT, "offset": offset, "reverseOrder": "false"}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=httpx.Timeout(timeout, connect=5.0))
        except httpx.HTTPError as exc:
            raise PumpThreadError(f"replies request failed: {exc}") from exc
        if r.status_code in (401, 403):
            # Token expired mid-scan: drop the cache so next refresh re-logins.
            global _cached_token
            _cached_token = None
            raise PumpThreadError(f"replies HTTP {r.status_code} (token rejected)")
        if r.status_code != 200:
            raise PumpThreadError(f"replies HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as exc:
            raise PumpThreadError(f"replies not JSON: {exc}") from exc

        if isinstance(data, dict):
            items = data.get("replies") or data.get("items") or data.get("data") or []
        else:
            items = data
        if not items:
            break
        before = len(out)
        for msg in items:
            if not isinstance(msg, dict):
                continue
            wallet = _wallet_of(msg)
            if wallet:
                out.add(wallet)
        # Short page = last page; no new wallets = offset-ignoring API — stop.
        if len(items) < _PAGE_LIMIT or len(out) == before:
            break
        offset += _PAGE_LIMIT
    return out


def load_allowlist(path: str | None = None) -> set[str]:
    """Manual callout allowlist (operator-controlled fallback). Never raises."""
    return _load_allowlist_file(path or config.CALLOUT_ALLOWLIST_PATH)
