"""Low-level Solana transaction primitives, built directly on `solders`.

We deliberately do NOT depend on `solana-py` / `spl-token` — only `solders`
(per handoff-tech). SPL Token / Associated-Token-Account / Compute-Budget
instructions are therefore hand-encoded here from their stable wire formats.

Everything money-critical funnels through `rpc.py` (fail-CLOSED). This module
only *builds* and *submits* transactions; it never decides amounts.

DRY_RUN: `build_and_send` / `send_tx_b64` short-circuit when `config.DRY_RUN`
is set — they log the intended action and return None instead of submitting.
"""
from __future__ import annotations

import base64
from typing import Optional

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer as _sys_transfer
from solders.transaction import Transaction

from . import config, rpc

# Stable program ids.
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

# SPL Token instruction tags (identical for SPL Token and Token-2022).
_TAG_TRANSFER_CHECKED = 12
_TAG_CLOSE_ACCOUNT = 9
# Associated-Token-Account program instruction tags.
_ATA_TAG_CREATE_IDEMPOTENT = 1
# Compute-budget instruction tags.
_CB_TAG_SET_CU_LIMIT = 2
_CB_TAG_SET_CU_PRICE = 3

# Assumed compute-unit ceiling used to convert a flat SOL priority fee into a
# per-CU micro-lamport price. Generous enough for a claim/close composite tx.
_CU_LIMIT = 250_000


# ---- PDA / ATA derivation ----

def event_authority_pda(program: Pubkey) -> Pubkey:
    """The `__event_authority` PDA every pump.fun ix carries (Anchor events)."""
    pda, _ = Pubkey.find_program_address([b"__event_authority"], program)
    return pda


def derive_ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    """Associated Token Account address. The token program is part of the seed,
    so SPL-Token and Token-2022 ATAs for the same owner+mint differ — passing
    the wrong program is the classic phantom-zero-balance bug.
    """
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        config.ASSOC_TOKEN_PROGRAM,
    )
    return pda


# ---- instruction builders ----

def priority_fee_ixs() -> list[Instruction]:
    """A SetComputeUnitLimit + SetComputeUnitPrice pair encoding PRIORITY_FEE_SOL.

    Helps txs land during congestion (stream-day spikes). Returns [] if the
    configured fee is zero.
    """
    fee_lamports = int(config.PRIORITY_FEE_SOL * 1_000_000_000)
    if fee_lamports <= 0:
        return []
    micro_lamports_per_cu = max(1, fee_lamports * 1_000_000 // _CU_LIMIT)
    limit_data = bytes([_CB_TAG_SET_CU_LIMIT]) + _CU_LIMIT.to_bytes(4, "little")
    price_data = bytes([_CB_TAG_SET_CU_PRICE]) + int(micro_lamports_per_cu).to_bytes(8, "little")
    return [
        Instruction(COMPUTE_BUDGET_PROGRAM, limit_data, []),
        Instruction(COMPUTE_BUDGET_PROGRAM, price_data, []),
    ]


def ix_create_idempotent_ata(
    funder: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey
) -> Instruction:
    """CreateIdempotent on the ATA program — no-op if the ATA already exists,
    so it is safe to prepend before every transfer without a pre-check.
    """
    ata = derive_ata(owner, mint, token_program)
    metas = [
        AccountMeta(funder, is_signer=True, is_writable=True),
        AccountMeta(ata, is_signer=False, is_writable=True),
        AccountMeta(owner, is_signer=False, is_writable=False),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(config.SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(token_program, is_signer=False, is_writable=False),
    ]
    return Instruction(config.ASSOC_TOKEN_PROGRAM, bytes([_ATA_TAG_CREATE_IDEMPOTENT]), metas)


def ix_transfer_checked(
    *,
    source: Pubkey,
    mint: Pubkey,
    dest: Pubkey,
    owner: Pubkey,
    amount: int,
    decimals: int,
    token_program: Pubkey,
) -> Instruction:
    """TransferChecked — the decimals-and-mint-verified transfer. We always use
    the checked variant so a wrong-decimals or wrong-mint bug aborts on-chain
    instead of moving the wrong quantity.
    """
    data = bytes([_TAG_TRANSFER_CHECKED]) + int(amount).to_bytes(8, "little") + bytes([decimals])
    metas = [
        AccountMeta(source, is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(dest, is_signer=False, is_writable=True),
        AccountMeta(owner, is_signer=True, is_writable=False),
    ]
    return Instruction(token_program, data, metas)


def ix_close_account(
    *, account: Pubkey, dest: Pubkey, owner: Pubkey, token_program: Pubkey
) -> Instruction:
    """CloseAccount — used to unwrap a temporary WSOL ATA back to native SOL
    after collecting AMM creator fees. Rent + balance go to `dest`.
    """
    metas = [
        AccountMeta(account, is_signer=False, is_writable=True),
        AccountMeta(dest, is_signer=False, is_writable=True),
        AccountMeta(owner, is_signer=True, is_writable=False),
    ]
    return Instruction(token_program, bytes([_TAG_CLOSE_ACCOUNT]), metas)


def ix_transfer_sol(*, from_pubkey: Pubkey, to_pubkey: Pubkey, lamports: int) -> Instruction:
    """Native SOL System-program transfer — used for the operator cut."""
    return _sys_transfer(TransferParams(from_pubkey=from_pubkey, to_pubkey=to_pubkey, lamports=lamports))


# ---- build / submit ----

def build_tx_b64(instructions: list[Instruction], blockhash: str) -> str:
    """Sign `instructions` with the bot wallet against `blockhash` and return a
    base64 wire transaction ready for sendTransaction.
    """
    bh = Hash.from_string(blockhash)
    msg = Message.new_with_blockhash(instructions, config.WALLET_PUBKEY, bh)
    tx = Transaction([config.WALLET_KEYPAIR], msg, bh)
    return base64.b64encode(bytes(tx)).decode("ascii")


def send_tx_b64(b64_tx: str, *, label: str, skip_preflight: bool = True) -> Optional[str]:
    """Submit a prebuilt wire tx. Returns signature, or None under DRY_RUN."""
    if config.DRY_RUN:
        print(f"[tx] DRY_RUN: would send {label} ({len(b64_tx)} b64 bytes)")
        return None
    sig = rpc.send_raw_tx(b64_tx, skip_preflight=skip_preflight)
    print(f"[tx] sent {label}: {sig}")
    return sig


def build_and_send(
    instructions: list[Instruction], *, label: str, add_priority: bool = True
) -> Optional[str]:
    """Convenience: prepend a priority fee, fetch a fresh blockhash, sign, send.

    Under DRY_RUN nothing is fetched or sent — we only log. Returns the
    signature on success, or None (DRY_RUN). Raises RPCError on submit failure.
    """
    full = (priority_fee_ixs() if add_priority else []) + instructions
    if config.DRY_RUN:
        print(f"[tx] DRY_RUN: would build+send {label} ({len(full)} ix)")
        return None
    blockhash = rpc.get_recent_blockhash()
    return send_tx_b64(build_tx_b64(full, blockhash), label=label)
