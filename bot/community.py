"""Community-engagement source — which wallets posted in the coin's community.

The engagement gate (config.ENGAGEMENT_GATE) requires a holder to have posted in
the coin's community on coincommunities.org before they're paid. Two sources,
merged:

  * the coin-communities server API for our mint   (auto, best-effort)
  * a manual allowlist file on the data volume      (reliable fallback/override)

AUTH (verified live): server key + secret (cck_… / ccs_…) are sent as the
`x-server-key` / `x-server-secret` headers — NOT `x-api-key` (that's the separate
browser key). Endpoint (server-side, reads every message of one community):

    GET {base}/api/v1/communities/{token_address}/messages/server

What counts as engagement (verified against the live schema): ONLY a genuine
top-level POST by the wallet in OUR community. Each message has `walletAddress`
(author), `parentMessageId` (null = post, non-null = reply), `tokenAddress` (the
community), and `deletedAt`. So we count a wallet iff it authored a message with
`parentMessageId is None`, `deletedAt is None`, and `tokenAddress == LOYALTY_MINT`.
Likes are a counter on a message, never their own record, so they can't be miscounted;
replies and deleted posts are excluded.

SAFETY MODEL (mirrors the rest of the engine):
  * `fetch_engaged_from_api` RAISES on any network/HTTP/parse failure. The caller
    (cycle.py) catches it and proceeds fail-SAFE: it keeps every wallet's existing
    sticky `engaged` flag and just skips this refresh. A flaky API must NEVER drop
    a holder who already earned engagement.
  * We never fabricate engagement; a wallet is engaged only if it posted (in the
    feed) or is on the allowlist. Engagement, once earned, is permanent.
"""
from __future__ import annotations

import json
import os

import httpx

from . import config


class CommunityError(RuntimeError):
    """Raised on any failure to read the community feed (caller fails SAFE)."""


# {token} is substituted with the mint. Override via COINCOMMUNITIES_MESSAGES_PATH.
_MESSAGES_PATH = "/api/v1/communities/{token}/messages/server"
_WALLET_FIELDS = ("walletAddress", "wallet", "authorWallet", "author", "userWallet")
_PAGE_LIMIT = 100
_MAX_PAGES = 50               # hard cap so a misbehaving API can't loop forever


def _messages_path(token: str) -> str:
    tpl = os.environ.get("COINCOMMUNITIES_MESSAGES_PATH") or _MESSAGES_PATH
    return tpl.replace("{token}", token)


def _wallet_of(msg: dict) -> str | None:
    for k in _WALLET_FIELDS:
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        # some payloads nest the author object
        if isinstance(v, dict):
            inner = v.get("walletAddress") or v.get("address") or v.get("wallet")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def fetch_engaged_from_api(
    token_address: str,
    *,
    base: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    timeout: float = 12.0,
) -> set[str]:
    """Return the set of wallet addresses that posted in `token_address`'s
    community, read from the coin-communities server messages endpoint. Raises
    CommunityError on any failure (missing creds, HTTP error, bad payload) so the
    caller can fail SAFE.
    """
    base = (base if base is not None else config.COINCOMMUNITIES_API_BASE).rstrip("/")
    api_key = api_key if api_key is not None else config.COINCOMMUNITIES_API_KEY
    api_secret = api_secret if api_secret is not None else config.COINCOMMUNITIES_API_SECRET
    if not api_key or not api_secret:
        raise CommunityError("COINCOMMUNITIES_API_KEY / _SECRET not set")

    url = base + _messages_path(token_address)
    headers = {"x-server-key": api_key, "x-server-secret": api_secret, "accept": "application/json"}
    out: set[str] = set()
    offset = 0
    for _ in range(_MAX_PAGES):
        params = {"limit": _PAGE_LIMIT, "offset": offset}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=httpx.Timeout(timeout, connect=5.0))
        except httpx.HTTPError as exc:
            raise CommunityError(f"messages request failed: {exc}") from exc
        if r.status_code != 200:
            raise CommunityError(f"messages HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as exc:
            raise CommunityError(f"messages not JSON: {exc}") from exc

        if isinstance(data, dict):
            items = data.get("messages") or data.get("items") or data.get("data") or []
        else:
            items = data
        if not items:
            break
        before = len(out)
        for msg in items:
            if not isinstance(msg, dict):
                continue
            # ONLY a genuine top-level POST counts — not a like (likes are a
            # counter, never a message), not a reply, not a deleted post, and not
            # a message from another coin's community.
            if msg.get("parentMessageId") is not None:   # reply, not a post
                continue
            if msg.get("deletedAt") is not None:          # post was removed
                continue
            ta = msg.get("tokenAddress")
            if ta is not None and ta != token_address:    # scope to OUR mint
                continue
            wallet = msg.get("walletAddress") or _wallet_of(msg)
            if wallet:
                out.add(wallet)
        # Stop on a short page, or if a (possibly offset-ignoring) API stops
        # yielding new wallets — belt-and-suspenders against an infinite loop.
        if len(items) < _PAGE_LIMIT or len(out) == before:
            break
        offset += _PAGE_LIMIT
    return out


def load_allowlist(path: str | None = None) -> set[str]:
    """Read the manual engaged-allowlist file. Never raises (returns {} on any
    problem) — it's the reliable, operator-controlled fallback. Accepts a JSON
    list, a JSON object with a `wallets` array, or a plain newline list
    (`#` comments allowed)."""
    path = path or config.ENGAGED_ALLOWLIST_PATH
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return set()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(x).strip() for x in data if str(x).strip()}
        if isinstance(data, dict):
            return {str(x).strip() for x in (data.get("wallets") or []) if str(x).strip()}
    except json.JSONDecodeError:
        return {ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}
    return set()
