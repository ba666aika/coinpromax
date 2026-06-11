"""Boot smoke test: `python -m bot` must reach its banner and the first tick.

Regression for the DISTRIBUTE_PCT incident: the boot banner referenced a config
attribute removed in the CPM split rework, so the bot crashed at startup — and
no unit test imported __main__, so the suite stayed green. This test launches
the real entrypoint as a subprocess (pointed at an unreachable RPC, so the
tick fail-closes) and asserts the banner printed and the process survived.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from solders.keypair import Keypair as _TestKp

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


class TestBotBoots(unittest.TestCase):
    def test_entrypoint_banner_and_first_tick(self):
        env = dict(os.environ)
        env.update(
            {
                "HELIUS_API_KEY": "test",
                "RPC_URL": "http://127.0.0.1:1",  # nothing listens → fail-CLOSED tick
                "WALLET_PRIVATE_KEY": str(_TestKp()),
                "LOYALTY_MINT": "2jCt3hj9vd7YpV7Sr3VA5nk3tdSpJtZezeoJXW4Xpump",
                "OPERATOR_WALLET": str(_TestKp().pubkey()),
                "DATA_DIR": tempfile.mkdtemp(),
                "DRY_RUN": "1",
                "CYCLE_INTERVAL_SECONDS": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        try:
            out = subprocess.run(
                [sys.executable, "-u", "-m", "bot"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=6,
            )
            text = (out.stdout or "") + (out.stderr or "")
            # If it exited on its own within the window, it crashed — fail loudly.
            self.fail(f"bot exited rc={out.returncode} within 6s:\n{text[-2000:]}")
        except subprocess.TimeoutExpired as e:
            def _s(v) -> str:  # TimeoutExpired yields bytes on some Pythons
                return v.decode() if isinstance(v, bytes) else (v or "")
            text = _s(e.stdout) + _s(e.stderr)

        self.assertIn("[bot] booted.", text)
        self.assertIn("[bot] split:", text)          # banner fully rendered
        self.assertIn("fail-CLOSED", text)            # first tick ran and fail-closed
        self.assertNotIn("Traceback", text)           # no crash


if __name__ == "__main__":
    unittest.main()
