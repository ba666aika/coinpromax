"""Raw Solana JSON-RPC client. Fail-CLOSED on any network/parse failure —
callers MUST handle `None` as "abort this cycle". Never return `0` on
failure (Jobcoin drain). All money-critical balance reads route through here.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from . import config


class RPCError(RuntimeError):
    """Raised when an RPC call fails in a money-critical path.

    Callers in `cycle.py` translate this into "skip the cycle entirely" —
    NEVER into "treat as zero".
    """


_client: Optional[httpx.Client] = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))
    return _client


def call(method: str, params: list[Any]) -> Any:
    """Returns the JSON-RPC `result`. Raises RPCError on any failure.

    Use this for non-money-critical lookups where you'd rather fail
    loudly than silently substitute defaults.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = client().post(config.RPC_URL, json=payload)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RPCError(f"{method}: transport/parse failed: {exc}") from exc
    if "error" in body:
        raise RPCError(f"{method}: rpc error: {body['error']}")
    return body.get("result")


def get_sol_balance(pubkey: str) -> int:
    """SOL balance in lamports for `pubkey`.

    Raises RPCError on any failure — DO NOT catch this and return 0
    in money-critical code (Jobcoin drain).
    """
    result = call("getBalance", [pubkey, {"commitment": "confirmed"}])
    try:
        return int(result["value"])
    except (TypeError, KeyError, ValueError) as exc:
        raise RPCError(f"getBalance: malformed result {result!r}: {exc}") from exc


def get_token_supply(mint: str) -> int:
    """Total supply of `mint` in raw units. Raises RPCError on failure."""
    result = call("getTokenSupply", [mint, {"commitment": "confirmed"}])
    try:
        return int(result["value"]["amount"])
    except (TypeError, KeyError, ValueError) as exc:
        raise RPCError(f"getTokenSupply: malformed result {result!r}: {exc}") from exc


def get_token_decimals(mint: str) -> int:
    result = call("getTokenSupply", [mint, {"commitment": "confirmed"}])
    try:
        return int(result["value"]["decimals"])
    except (TypeError, KeyError, ValueError) as exc:
        raise RPCError(f"getTokenSupply.decimals: malformed: {exc}") from exc


def get_account_info(pubkey: str, *, encoding: str = "base64") -> Optional[dict[str, Any]]:
    """Raw account info. Returns the `value` dict, or None if the account does
    not exist. Raises RPCError on transport/parse failure (fail-CLOSED — a
    network blip must NOT be read as "account absent").
    """
    result = call("getAccountInfo", [pubkey, {"encoding": encoding, "commitment": "confirmed"}])
    # result is {"context": ..., "value": null|{...}}. value==None => no account.
    if not isinstance(result, dict):
        raise RPCError(f"getAccountInfo: malformed result {result!r}")
    return result.get("value")


def get_token_program(mint: str) -> str:
    """Return the owning token-program id of `mint` (SPL Token vs Token-2022).

    pump.fun has minted under both programs over time, and the program id is
    part of the ATA seed — guessing wrong yields phantom-zero balances and
    failed transfers. Detect it from the mint account's `owner`. Fail-CLOSED.
    """
    value = get_account_info(mint)
    if not value:
        raise RPCError(f"get_token_program: mint {mint} not found")
    owner = value.get("owner")
    if owner not in (str(config.TOKEN_PROGRAM), str(config.TOKEN_2022_PROGRAM)):
        raise RPCError(f"get_token_program: mint {mint} owner {owner!r} is not a token program")
    return owner


def get_spl_balance(owner_token_account: str) -> int:
    """Raw token balance of a specific token account. Fail-CLOSED.

    Used for buyback/distribute deltas — `bought = after - before` of the
    bot's own $LOYALTY ATA. Never trust quote/program return values.
    """
    result = call("getTokenAccountBalance", [owner_token_account, {"commitment": "confirmed"}])
    try:
        return int(result["value"]["amount"])
    except (TypeError, KeyError, ValueError) as exc:
        raise RPCError(f"getTokenAccountBalance: malformed result {result!r}: {exc}") from exc


def get_signature_statuses(signatures: list[str]) -> list[Optional[dict[str, Any]]]:
    """Bulk-confirm signatures (up to 256 per call). Returns the per-sig status
    list (None entry = unknown/not-yet-seen). Raises RPCError on transport
    failure. This is NOT money-critical on its own — it only reports outcomes
    of already-sent transfers — but we still surface failures to the caller.
    """
    result = call("getSignatureStatuses", [signatures, {"searchTransactionHistory": False}])
    try:
        value = result["value"]
    except (TypeError, KeyError) as exc:
        raise RPCError(f"getSignatureStatuses: malformed result {result!r}: {exc}") from exc
    if not isinstance(value, list):
        raise RPCError(f"getSignatureStatuses: expected list, got {type(value).__name__}")
    return value


def get_token_holders(mint: str, program: Optional[str] = None) -> list[dict[str, Any]]:
    """All accounts holding `mint`, via getProgramAccounts on the mint's own
    token program. Returns [{pubkey, owner, amount}], zero balances filtered.

    `program` is the token-program id owning the mint. If None it is detected
    (one extra getAccountInfo). Detecting is important: Token-2022 ATAs carry
    the ImmutableOwner extension, so they are LARGER than 165 bytes — the old
    `dataSize: 165` filter silently dropped them. We filter by mint memcmp
    only and guard on parsed `type == "account"`.

    Raises RPCError on failure. Caller treats this as fail-CLOSED — skip the
    cycle entirely if holders can't be enumerated (never proceed with a partial
    snapshot; that would mass-reset held_seconds — see loyalty_tracker docstring).

    NOTE: one call per mint. pump.fun coins rarely exceed a few thousand holders,
    which fits a single getProgramAccounts. Add pagination if traffic grows.
    """
    if program is None:
        program = get_token_program(mint)
    params = [
        program,
        {
            "encoding": "jsonParsed",
            "commitment": "confirmed",
            "filters": [
                {"memcmp": {"offset": 0, "bytes": mint}},
            ],
        },
    ]
    result = call("getProgramAccounts", params)
    if not isinstance(result, list):
        raise RPCError(f"getProgramAccounts: expected list, got {type(result).__name__}")

    holders: list[dict[str, Any]] = []
    for entry in result:
        try:
            parsed = entry["account"]["data"]["parsed"]
            if parsed.get("type") != "account":
                continue  # skip the mint account itself / non-token-account data
            info = parsed["info"]
            amount = int(info["tokenAmount"]["amount"])
            if amount <= 0:
                continue
            holders.append(
                {
                    "pubkey": entry["pubkey"],
                    "owner": info["owner"],
                    "amount": amount,
                }
            )
        except (TypeError, KeyError, ValueError) as exc:
            # One malformed entry doesn't poison the whole cycle, but we DO
            # log it loudly. If the failure rate is high, that's a real issue.
            print(f"[rpc] WARN: skipping malformed token account: {exc}")
    return holders


def get_account_owner_programs(pubkeys: list[str]) -> dict[str, Optional[str]]:
    """Map each pubkey -> the program that OWNS its account (None if it doesn't
    exist). Batched via getMultipleAccounts (<=100 per call).

    Used to tell real wallets (owned by the System Program) from pools / PDAs /
    bonding curves (owned by a program), so AMM/LP pools are auto-excluded from
    distribution. Raises RPCError on transport/parse failure — the caller treats
    that as "skip auto-detection this tick" (fail-SAFE; never an excuse to pay a
    pool, since the static + already-discovered exclusions still apply).
    """
    out: dict[str, Optional[str]] = {}
    for i in range(0, len(pubkeys), 100):
        batch = pubkeys[i : i + 100]
        result = call("getMultipleAccounts", [batch, {"encoding": "base64", "commitment": "confirmed"}])
        try:
            value = result["value"]
        except (TypeError, KeyError) as exc:
            raise RPCError(f"getMultipleAccounts: malformed result {result!r}: {exc}") from exc
        if not isinstance(value, list) or len(value) != len(batch):
            raise RPCError(f"getMultipleAccounts: expected {len(batch)} entries, got {value!r}")
        for pk, acc in zip(batch, value):
            out[pk] = acc.get("owner") if isinstance(acc, dict) else None
    return out


def get_recent_blockhash() -> str:
    """Latest blockhash. Used for tx construction (one per chunk in distribute)."""
    result = call("getLatestBlockhash", [{"commitment": "confirmed"}])
    try:
        return result["value"]["blockhash"]
    except (TypeError, KeyError) as exc:
        raise RPCError(f"getLatestBlockhash: malformed: {exc}") from exc


def send_raw_tx(b64_tx: str, *, skip_preflight: bool = True) -> str:
    """Submit a base64-encoded signed transaction. Returns the signature.

    `skipPreflight=true` is REQUIRED on Helius beta (per handoff-tech).
    Raises RPCError on submission failure.
    """
    result = call(
        "sendTransaction",
        [
            b64_tx,
            {"encoding": "base64", "skipPreflight": skip_preflight, "preflightCommitment": "confirmed"},
        ],
    )
    if not isinstance(result, str):
        raise RPCError(f"sendTransaction: expected signature str, got {type(result).__name__}")
    return result
