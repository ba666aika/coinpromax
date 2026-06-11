"""Claim pump.fun creator fees — bonding curve AND PumpSwap AMM.

Account layouts are encoded directly from the official pump-fun IDL
(`pump-fun/pump-public-docs`, idl/pump.json + idl/pump_amm.json), not guessed:

  bonding `collect_creator_fee`  (program 6EF8…): native SOL → creator wallet.
  AMM     `collect_coin_creator_fee` (program pAMM…): WSOL → a temp WSOL ATA we
          create, then close to unwrap into native SOL.

Two INDEPENDENT transactions (failure isolation): a coin pre-migration has only
bonding-curve fees; post-migration it accrues AMM fees too. Each function logs
and returns None on its own failure so one cannot block the other.

CRITICAL: nothing here reports an amount. `claimed = wallet_sol_delta` is
computed by the caller (cycle.py) around these calls. The program return value
is never trusted (see [[incidents]] / Jobcoin).
"""
from __future__ import annotations

from typing import Optional

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from . import config, rpc, solana_tx as stx
from .rpc import RPCError

# Anchor discriminators (sha256("global:<ix>")[:8]) straight from the IDL.
_DISC_COLLECT_CREATOR_FEE = bytes([20, 22, 86, 123, 198, 28, 219, 132])
_DISC_COLLECT_COIN_CREATOR_FEE = bytes([160, 57, 89, 42, 181, 139, 43, 66])


def claim_bonding_curve() -> Optional[str]:
    """Collect bonding-curve creator fees (native SOL straight to our wallet).

    Safe to call every cycle: if the vault is empty the program transfers 0.
    Returns the signature, None on DRY_RUN, or None on a handled failure.
    """
    creator = config.WALLET_PUBKEY
    prog = config.PUMPFUN_BONDING_PROGRAM
    creator_vault, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)], prog  # NOTE: hyphen on the bonding side
    )
    metas = [
        AccountMeta(creator, is_signer=True, is_writable=True),
        AccountMeta(creator_vault, is_signer=False, is_writable=True),
        AccountMeta(config.SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(stx.event_authority_pda(prog), is_signer=False, is_writable=False),
        AccountMeta(prog, is_signer=False, is_writable=False),
    ]
    ix = Instruction(prog, _DISC_COLLECT_CREATOR_FEE, metas)
    try:
        return stx.build_and_send([ix], label="claim_bonding_curve")
    except RPCError as exc:
        print(f"[pumpfun] bonding-curve claim failed (isolated): {exc}")
        return None


def claim_amm() -> Optional[str]:
    """Collect PumpSwap AMM creator fees. Fees accrue in WSOL inside the coin's
    creator-vault ATA; we collect into our own temporary WSOL ATA and close it
    in the same tx so the SOL lands natively in the wallet.

    Skips entirely (returns None) if the coin has no AMM creator-vault ATA yet
    (pre-migration) — avoids burning fees on a tx that would just fail.
    """
    creator = config.WALLET_PUBKEY
    prog = config.PUMPSWAP_AMM_PROGRAM
    wsol = config.WSOL_MINT
    wsol_program = config.TOKEN_PROGRAM  # WSOL is a classic SPL Token mint

    vault_authority, _ = Pubkey.find_program_address(
        [b"creator_vault", bytes(creator)], prog  # NOTE: underscore on the AMM side
    )
    vault_ata = stx.derive_ata(vault_authority, wsol, wsol_program)
    our_wsol_ata = stx.derive_ata(creator, wsol, wsol_program)

    # Pre-flight: only attempt if the vault ATA exists (there is something to claim).
    try:
        if rpc.get_account_info(str(vault_ata)) is None:
            return None  # no AMM fees yet — nothing to do
    except RPCError as exc:
        # Fail-CLOSED on the read: don't attempt a claim against unknown state.
        print(f"[pumpfun] AMM vault preflight failed (isolated): {exc}")
        return None

    collect_metas = [
        AccountMeta(wsol, is_signer=False, is_writable=False),
        AccountMeta(wsol_program, is_signer=False, is_writable=False),
        AccountMeta(creator, is_signer=False, is_writable=False),
        AccountMeta(vault_authority, is_signer=False, is_writable=False),
        AccountMeta(vault_ata, is_signer=False, is_writable=True),
        AccountMeta(our_wsol_ata, is_signer=False, is_writable=True),
        AccountMeta(stx.event_authority_pda(prog), is_signer=False, is_writable=False),
        AccountMeta(prog, is_signer=False, is_writable=False),
    ]
    ixs = [
        # Ensure our WSOL ATA exists to receive the collected fees.
        stx.ix_create_idempotent_ata(creator, creator, wsol, wsol_program),
        Instruction(prog, _DISC_COLLECT_COIN_CREATOR_FEE, collect_metas),
        # Unwrap: closing the WSOL ATA returns its lamports (incl. collected
        # fees) to the wallet as native SOL.
        stx.ix_close_account(account=our_wsol_ata, dest=creator, owner=creator, token_program=wsol_program),
    ]
    try:
        return stx.build_and_send(ixs, label="claim_amm")
    except RPCError as exc:
        print(f"[pumpfun] AMM claim failed (isolated): {exc}")
        return None
