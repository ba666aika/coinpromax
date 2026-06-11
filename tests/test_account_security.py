"""Frontend read-only regression for the Coin Pro Max site.

Coin Pro Max is a marketing scroll-site + read-only "check wallet" lookup. It
never connects a wallet and never asks the visitor to sign anything: levels and
shares are computed server-side from on-chain state and served over the
key-less /api/* endpoints. These tests fail the build if first-party frontend
code starts touching a wallet.
"""
from __future__ import annotations

import ast
import os
import re
import unittest


REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
FRONTEND = os.path.join(REPO_ROOT, "frontend")

# Wallet-touching call patterns. A read-only site must never invoke these.
FORBIDDEN = (
    ".signTransaction",
    ".signAndSendTransaction",
    ".signAllTransactions",
    ".signMessage",
    "window.solana",
    "window.phantom",
)


def _first_party_sources() -> list[str]:
    """Every .html/.js under frontend/ — all first-party (no vendor bundles in
    this repo; if one is ever added, exclude it here explicitly)."""
    out: list[str] = []
    for root, _dirs, files in os.walk(FRONTEND):
        for name in files:
            if name.endswith((".html", ".js")):
                out.append(os.path.join(root, name))
    return out


def _read_code_only(fs_path: str) -> str:
    """Strip // and /* */ comments so we catch real calls, not prose."""
    with open(fs_path, "r", encoding="utf-8") as f:
        src = f.read()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src


class TestFrontendIsReadOnly(unittest.TestCase):
    def test_frontend_has_sources(self):
        self.assertTrue(_first_party_sources(), "frontend/ is empty?")

    def test_first_party_code_never_touches_a_wallet(self):
        for path in _first_party_sources():
            src = _read_code_only(path)
            for forbidden in FORBIDDEN:
                self.assertNotIn(forbidden, src, f"{forbidden!r} called from {path}")

    def test_frontend_only_reads_the_api(self):
        # The live widgets must only GET the public API, never mutate.
        for path in _first_party_sources():
            src = _read_code_only(path)
            for forbidden in ("method:'POST'", 'method: "POST"', "method:'PUT'", "method:'DELETE'"):
                self.assertNotIn(forbidden, src, f"{forbidden!r} in {path} (read-only only)")


def _imported_modules(rel_path: str) -> set:
    """Every module name imported by a source file (absolute + relative)."""
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    mods: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            mods.add("." * node.level + (node.module or ""))
    return mods


class TestWebServerIsKeyless(unittest.TestCase):
    """The read-only web server must never gain the ability to move money.

    server.py listens on a socket; only the bot holds the wallet key. If a
    refactor pulls a money module (swap / distribute / pumpfun / solana_tx) or
    bot.config (which loads WALLET_PRIVATE_KEY) into server.py, the
    network-exposed surface becomes one hop from the wallet — exactly the
    Jobcoin open-endpoint drain. Fail the build if that ever happens.
    """

    MONEY = {
        "swap", "distribute", "pumpfun", "solana_tx", "config", "cycle",
        "stocks", "pumpthread",
        "bot.swap", "bot.distribute", "bot.pumpfun", "bot.solana_tx",
        "bot.config", "bot.cycle", "bot.stocks", "bot.pumpthread",
    }

    def test_server_imports_at_most_bot_ranking(self):
        mods = _imported_modules("server.py")
        bot_mods = {m for m in mods if m == "bot" or m.startswith("bot.")}
        self.assertLessEqual(
            bot_mods, {"bot.ranking"},
            f"web server may import at most bot.ranking (pure math), found: {sorted(bot_mods)}",
        )
        self.assertEqual(
            mods & self.MONEY, set(),
            "web server imports a money/key module — Jobcoin open-endpoint risk",
        )

    def test_server_handles_no_mutating_http_methods(self):
        # Read-only: do_GET only. A do_POST/PUT/DELETE handler is a mutation
        # surface and must not exist on the key-adjacent process.
        with open(os.path.join(REPO_ROOT, "server.py"), encoding="utf-8") as f:
            src = f.read()
        for verb in ("def do_POST", "def do_PUT", "def do_DELETE", "def do_PATCH"):
            self.assertNotIn(verb, src, f"web server defines {verb} (must be read-only)")

    def test_ranking_pulls_in_no_engine_modules(self):
        mods = _imported_modules("bot/ranking.py")
        leaked = {m for m in mods if m.startswith("bot") or m.startswith(".") or m in self.MONEY}
        self.assertEqual(
            leaked, set(),
            f"bot.ranking must stay pure (no engine/config imports), found: {sorted(leaked)}",
        )


if __name__ == "__main__":
    unittest.main()
