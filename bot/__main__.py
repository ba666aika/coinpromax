"""Bot entry point — invoked by `python -m bot` from start.py.

Boots the main loop. Catastrophic errors propagate up to start.py,
which tears down the web sibling and exits non-zero so Railway restarts.
"""
from __future__ import annotations

import sys
import time
import traceback

from . import config
from .cycle import tick


def main() -> None:
    print(f"[bot] booted. wallet={config.WALLET_PUBKEY}  mint={config.LOYALTY_MINT}")
    print(
        f"[bot] cadence cycle={config.CYCLE_INTERVAL_SECONDS}s  "
        f"airdrop={config.AIRDROP_INTERVAL_SECONDS}s  "
        f"buyback_cap={config.MAX_BUYBACK_LAMPORTS} lamports"
    )
    print(f"[bot] split: operator {config.OPERATOR_PCT * 100:.0f}% / holders {config.DISTRIBUTE_PCT * 100:.0f}%")

    while True:
        try:
            tick()
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001 — top-level guard
            # Single-tick failures must NEVER drop money behavior.
            # Log loudly and continue; the next tick will retry. The whole
            # process exits only on SystemExit (config validation, etc).
            print(f"[bot] tick failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exc()
        time.sleep(config.CYCLE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
