"""Read-only HTTP API + static frontend, on stdlib `http.server` only.

Endpoints (Coin Pro Max):
  GET /api/stats             — engine state (holders, pools, level tiers)
  GET /api/wallet?wallet=... — one wallet: level, tasks done, weight share
                               (drives the "check wallet" bar on the site)
  GET /api/board             — leaderboard by weight (held_seconds × balance)
  GET /                      — frontend/index.html
  GET /<static>              — anything under frontend/

CRITICAL: No HTTP endpoint moves money. The bot (`python -m bot`) is the
only thing that holds the private key and signs transactions. Everything here
is computed from JSON files under /data (written by the bot).

Cache headers per handoff-tech.md:
  - HTML / JSON  → no-store      (avoid stale views after deploy)
  - JS / CSS     → no-cache      (force revalidate, allow 304)
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

DATA_DIR = os.environ.get("DATA_DIR") or "/data"
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
PORT = int(os.environ.get("PORT") or 8080)

# How many bubbles the map renders. Past the top ~80 the rank-curve share is a
# fraction of a percent each, bubbles hit the minimum radius and overlap into an
# unreadable cloud (and the O(n²) physics on the client starts to drag on mobile).
# The full eligible count is still reported as `total_holders` for the counter.
HOLDERS_API_TOP_N = int(os.environ.get("HOLDERS_API_TOP_N") or 80)


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _wallet_level(info: dict, min_hold: int) -> int:
    """Mirror of bot.loyalty_tracker.level_of WITHOUT importing bot.config
    (this process is key-less by design). Level 1 = eligible holder with held
    time; +1 per sticky task. Just-reset / below-floor wallets are level 0."""
    held = int(info.get("held_seconds", 0))
    bal = int(info.get("last_balance", 0))
    if held <= 0 or bal < min_hold:
        return 0
    return 1 + len(info.get("tasks") or {})


class Handler(BaseHTTPRequestHandler):
    server_version = "coinpromax/0.1"

    # ---- header helpers ----

    def _cors_and_cache(self, ctype: str) -> None:
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        if ctype.startswith("text/html") or ctype.startswith("application/json"):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "no-cache")

    def _send_json(self, code: int, body: Any) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self._cors_and_cache("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, fs_path: str, ctype: str) -> None:
        try:
            with open(fs_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self._send_json(404, {"error": "not_found"})
            return
        self.send_response(200)
        self._cors_and_cache(ctype)
        # Relaxed CSP: this is a static marketing page (no wallet connect) that
        # uses inline scripts, CDN fonts, and the Twitter embed widget.
        if fs_path.endswith(".html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' https: data: blob: 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline' https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "font-src 'self' data: https:; "
                "img-src 'self' data: blob: https:; "
                "frame-src 'self' https:; connect-src 'self' https:",
            )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- routing ----

    def do_GET(self) -> None:  # noqa: N802  (stdlib name)
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        if path == "/api/stats":
            self._send_json(200, _load_json(os.path.join(DATA_DIR, "stats.json"), {}))
            return

        if path == "/api/meta":
            # Public, env-backed metadata for the frontend. Works even before
            # the bot has written any stats. LOYALTY_MINT is a public address.
            self._send_json(200, {
                "mint": (os.environ.get("LOYALTY_MINT") or "").strip(),
                "community_base": "https://coincommunities.org/communities/",
            })
            return

        if path == "/api/wallet":
            # The "check wallet" endpoint: level, tasks, share. A wallet's level
            # is 1 once it holds ≥ min without selling (held time > 0), +1 per
            # completed task (pump.fun call-out / communities bullpost). Any
            # sell resets the accumulated time → level 0 until it re-accrues.
            wallet = (qs.get("wallet") or [""])[0].strip()
            if not wallet:
                self._send_json(400, {"error": "missing_wallet"})
                return
            state = _load_json(os.path.join(DATA_DIR, "loyalty_state.json"), {})
            stats = _load_json(os.path.join(DATA_DIR, "stats.json"), {})
            min_hold = int(stats.get("min_holding_raw", 0))
            excluded = set(stats.get("excluded_owners", []))
            info = state.get(wallet)
            if not info:
                self._send_json(200, {
                    "wallet": wallet, "known": False, "level": 0,
                    "tasks": {"hold": False, "callout": False, "bullpost": False},
                })
                return
            # Weight map = held_seconds × balance over eligible wallets — the
            # exact weight every airdrop is paid on.
            wmap: dict[str, int] = {}
            for w, x in state.items():
                h = int(x.get("held_seconds", 0))
                b = int(x.get("last_balance", 0))
                if h > 0 and b >= min_hold and w not in excluded:
                    wmap[w] = h * b
            total_w = sum(wmap.values())
            me_w = wmap.get(wallet, 0)
            eligible = wallet in wmap
            rank = (1 + sum(1 for v in wmap.values() if v > me_w)) if eligible else None
            tasks = info.get("tasks") or {}
            self._send_json(
                200,
                {
                    "wallet": wallet,
                    "known": True,
                    "eligible": eligible,
                    "level": _wallet_level(info, min_hold),
                    # Lvl 4 (casino): all previous levels done → in every draw.
                    "casino_eligible": eligible and "callout" in tasks and "bullpost" in tasks,
                    "tasks": {
                        "hold": eligible,
                        "callout": "callout" in tasks,
                        "bullpost": "bullpost" in tasks,
                    },
                    "held_seconds": int(info.get("held_seconds", 0)),
                    "balance_raw": int(info.get("last_balance", 0)),
                    "first_seen_ts": int(info.get("first_seen_ts", 0)),
                    "rank": rank,
                    "share_bps": int(me_w * 10000 / total_w) if (eligible and total_w) else 0,
                },
            )
            return

        if path == "/api/board":
            # Leaderboard: proportional share = held_seconds × balance — the
            # exact weight the engine pays airdrops on. Includes each wallet's
            # level so the site can badge rows.
            state = _load_json(os.path.join(DATA_DIR, "loyalty_state.json"), {})
            stats = _load_json(os.path.join(DATA_DIR, "stats.json"), {})
            min_hold = int(stats.get("min_holding_raw", 0))
            excluded = set(stats.get("excluded_owners", []))
            wmap: dict[str, int] = {}
            for w, info in state.items():
                h = int(info.get("held_seconds", 0))
                b = int(info.get("last_balance", 0))
                if h <= 0 or b < min_hold or w in excluded:
                    continue
                wmap[w] = h * b
            total_w = sum(wmap.values())
            rows = []
            for rank, (w, weight) in enumerate(
                sorted(wmap.items(), key=lambda kv: kv[1], reverse=True), start=1
            ):
                info = state[w]
                rows.append({
                    "wallet": w,
                    "rank": rank,
                    "share_bps": int(weight * 10000 / total_w) if total_w else 0,
                    "held_seconds": int(info.get("held_seconds", 0)),
                    "balance": int(info.get("last_balance", 0)),
                    "level": _wallet_level(info, min_hold),
                })
            self._send_json(200, {"total_holders": len(wmap), "holders": rows[:HOLDERS_API_TOP_N]})
            return

        # Static.
        if path == "/" or path == "":
            self._send_file(os.path.join(FRONTEND_DIR, "index.html"), "text/html; charset=utf-8")
            return

        # Generic static: only files inside FRONTEND_DIR. Reject path traversal.
        safe = os.path.normpath(path.lstrip("/"))
        if safe.startswith("..") or os.path.isabs(safe):
            self._send_json(403, {"error": "forbidden"})
            return
        fs_path = os.path.join(FRONTEND_DIR, safe)
        if not os.path.isfile(fs_path):
            self._send_json(404, {"error": "not_found"})
            return
        ctype = "application/octet-stream"
        if safe.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        elif safe.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif safe.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif safe.endswith(".svg"):
            ctype = "image/svg+xml"
        elif safe.endswith(".png"):
            ctype = "image/png"
        elif safe.endswith(".jpg") or safe.endswith(".jpeg"):
            ctype = "image/jpeg"
        elif safe.endswith(".webp"):
            ctype = "image/webp"
        elif safe.endswith(".gif"):
            ctype = "image/gif"
        elif safe.endswith(".ico"):
            ctype = "image/x-icon"
        elif safe.endswith(".woff2"):
            ctype = "font/woff2"
        elif safe.endswith(".woff"):
            ctype = "font/woff"
        elif safe.endswith(".ttf"):
            ctype = "font/ttf"
        elif safe.endswith(".eot"):
            ctype = "application/vnd.ms-fontobject"
        elif safe.endswith(".mp4"):
            ctype = "video/mp4"
        elif safe.endswith(".webm"):
            ctype = "video/webm"
        elif safe.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        elif safe.endswith(".json"):
            ctype = "application/json"
        self._send_file(fs_path, ctype)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter access log
        print(f"[web] {self.address_string()} {fmt % args}")


def main() -> None:
    print(f"[web] listening on :{PORT}  frontend={FRONTEND_DIR}  data={DATA_DIR}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
