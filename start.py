"""Process launcher: runs the web (server.py) as a keepalive and SUPERVISES the
bot (python -m bot) alongside it, both sharing /data/.

Design:
  - The WEB is the container keepalive. It has NO external dependencies (no
    wallet key, no RPC), so it always boots and serves the read-only site. If the
    web ever dies, that's unexpected, so we exit non-zero and let Railway restart
    the whole container.
  - The BOT is supervised. If it exits — a transient RPC blip, or simply missing
    env before launch — we log it and RESTART it with exponential backoff instead
    of taking the site down with it. So the lander stays up even before the bot is
    fully configured, and a bot crash in production self-heals.

This replaces the old "if either process exits, kill both" behavior, which made
the whole site flap whenever the bot couldn't boot (e.g. before secrets are set).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import NoReturn

_shutting_down = False


def _spawn(args: list[str], env: dict) -> subprocess.Popen:
    return subprocess.Popen(args, env=env, stdout=sys.stdout, stderr=sys.stderr)


_STATE_FILES = (
    "loyalty_state.json",   # held_seconds + tasks per wallet — the CPM clock
    "stats.json",
    "payouts.jsonl",
    "last_airdrop_at.txt",
    "last_claim_at.txt",
    "engaged_allowlist.json",   # bullpost manual allowlist
    "callout_allowlist.json",   # callout manual allowlist
    "sol_pool.json",            # SOL-airdrop accumulator (new coin = new wallet = fresh books)
    "ad_reserve.json",          # ad-bounty reserve accounting
    "cpm_buy_pool.json",        # buyback budget accumulator
    "stock_buy_pool.json",      # stock-basket budget accumulator
    "casino_pool.json",         # lvl-4 casino pot
    "total_distributed.json",   # lifetime distributed counter
    "last_stocks_airdrop_at.txt",
    "last_casino_at.txt",
)


def _wipe_state(data_dir: str, reason: str) -> None:
    removed = []
    for name in _STATE_FILES:
        path = os.path.join(data_dir, name)
        try:
            os.remove(path)
            removed.append(name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[start] could not remove {path}: {exc}", flush=True)
    print(f"[start] WIPED state ({reason}): {removed or 'nothing to remove'}", flush=True)


def _maybe_wipe_state(data_dir: str) -> None:
    """Reset persisted state (held_seconds / stats / payouts) at container boot,
    BEFORE the bot loads it — so a wipe can never race the bot's per-tick save.

    Two triggers:
      1. WIPE_DATA=1 — explicit one-shot wipe (remove the var after it runs once).
      2. mint change — LOYALTY_MINT differs from the last launch. A new coin means
         a fresh loyalty clock; old holders are irrelevant. Tracked via
         <data>/mint.txt so it fires exactly once per new mint and never wipes an
         unchanged launch (no footgun on a normal redeploy).
    """
    if (os.environ.get("WIPE_DATA") or "").strip().lower() in ("1", "true", "yes", "on"):
        _wipe_state(data_dir, "WIPE_DATA set")

    mint = (os.environ.get("LOYALTY_MINT") or "").strip()
    if not mint:
        return
    marker = os.path.join(data_dir, "mint.txt")
    try:
        with open(marker, "r", encoding="utf-8") as f:
            prev = f.read().strip()
    except FileNotFoundError:
        prev = ""
    if prev and prev != mint:
        _wipe_state(data_dir, f"mint changed {prev[:8]}… -> {mint[:8]}…")
    if prev != mint:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(mint)
        except OSError as exc:
            print(f"[start] could not write mint marker {marker}: {exc}", flush=True)


def main() -> NoReturn:
    data_dir = os.environ.get("DATA_DIR") or "/data"
    # Ensure the data dir exists (Railway volume mount, may be absent in local dev).
    os.makedirs(data_dir, exist_ok=True)

    # Reset persisted state at boot if requested / on a new mint — BEFORE the bot
    # process loads it, so the wipe can't race the bot's per-tick save.
    _maybe_wipe_state(data_dir)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    # Defense in depth: the WEB is the only process that listens on a socket, and
    # it never needs a secret (it imports only bot.ranking, never bot.config). So
    # it gets an environment with every secret stripped out — an exploit of the
    # network-exposed process cannot read a key that simply isn't there. Keeps the
    # money-capable surface (bot) and the network-exposed surface (web) disjoint.
    # (Jobcoin lesson: an open endpoint must never be one hop from the wallet key.)
    _WEB_SECRET_DENYLIST = (
        "WALLET_PRIVATE_KEY",
        "HELIUS_API_KEY",
        "RPC_URL",                    # embeds the Helius key
        "COINCOMMUNITIES_API_KEY",
        "COINCOMMUNITIES_API_SECRET",
    )
    web_env = {k: v for k, v in env.items() if k not in _WEB_SECRET_DENYLIST}

    web_cmd = [sys.executable, "-u", "server.py"]
    bot_cmd = [sys.executable, "-u", "-m", "bot"]

    web = _spawn(web_cmd, web_env)
    bot = _spawn(bot_cmd, env)

    def shutdown(signum: int, _frame) -> None:
        global _shutting_down
        _shutting_down = True
        for p in (web, bot):
            try:
                p.send_signal(signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    backoff = 1.0
    bot_started = time.time()

    while not _shutting_down:
        # WEB is the keepalive — if it dies, restart the whole container.
        rc = web.poll()
        if rc is not None:
            print(f"[start] web exited with code {rc}; restarting container", flush=True)
            try:
                bot.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(2)
            sys.exit(rc if rc != 0 else 1)

        # BOT is supervised — restart it on exit, keep the site up.
        rc = bot.poll()
        if rc is not None:
            # If the bot had been running healthily for a while, reset the backoff.
            if time.time() - bot_started > 60:
                backoff = 1.0
            print(
                f"[start] bot exited with code {rc}; restarting in {backoff:.0f}s "
                f"(web stays up)",
                flush=True,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            if _shutting_down:
                break
            bot = _spawn(bot_cmd, env)
            bot_started = time.time()

        time.sleep(1)

    # Graceful shutdown: children already got the signal; give them a moment.
    time.sleep(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
