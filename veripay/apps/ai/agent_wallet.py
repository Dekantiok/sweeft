"""Server-side signing for the VeriPay AI co-pilot agent account (testnet demo).

The co-pilot holds no user keys. A user grants it an on-chain NEAR allowance
with ONE wallet signature (`approve(spender=agent, amount)` on the token);
the agent can then call `ft_transfer_from` and the token contract itself
enforces the budget — scoped, capped and revocable on-chain. That allowance
is the security boundary.

Trust disclosure (testnet MVP): `AI_AGENT_PRIVATE_KEY` lives on the demo
server because this is a testnet build. On mainnet the same key would sit
inside a TEE / KMS — the on-chain allowance design does not change. Even a
fully compromised agent key can never move more than the granted allowance,
and the user can revoke at any time by approving 0.

This module implements the minimal NEAR transaction pipeline in Python
(borsh encode -> ed25519 sign -> broadcast) so the demo needs no extra SDK.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings

from apps.escrow.near import _rpc
from apps.wallets.base58 import b58decode, b58encode

GAS_FUNCTION_CALL = 100_000_000_000_000  # 100 Tgas


def agent_configured() -> bool:
    return bool(
        getattr(settings, "AI_AGENT_ACCOUNT", "")
        and getattr(settings, "AI_AGENT_PRIVATE_KEY", "")
        and getattr(settings, "AI_AGENT_ENABLED", True)
    )


# ---------------------------------------------------------------------------
# Borsh primitives (NEAR transaction wire format)
# ---------------------------------------------------------------------------


def _borsh_string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


def _borsh_bytes(raw: bytes) -> bytes:
    return struct.pack("<I", len(raw)) + raw


def _parse_key(private_key: str) -> tuple[Ed25519PrivateKey, bytes]:
    raw = b58decode(private_key.split(":")[-1])
    if len(raw) != 64:
        raise RuntimeError("agent private key must be 64 bytes (ed25519: seed+public)")
    seed, public = raw[:32], raw[32:]
    return Ed25519PrivateKey.from_private_bytes(seed), public


def _function_call_action(method_name: str, args: dict, gas: int, deposit: int) -> bytes:
    action = b"\x02"  # Action::FunctionCall
    action += _borsh_string(method_name)
    action += _borsh_bytes(json.dumps(args).encode())
    action += struct.pack("<Q", gas)
    action += deposit.to_bytes(16, "little")
    return action


def _transfer_action(deposit: int) -> bytes:
    return b"\x03" + deposit.to_bytes(16, "little")  # Action::Transfer


def _build_transaction(
    *,
    signer_id: str,
    public_key: bytes,
    nonce: int,
    receiver_id: str,
    block_hash: bytes,
    action: bytes,
) -> bytes:
    tx = _borsh_string(signer_id)
    tx += b"\x00" + public_key  # ed25519 key type tag + 32 bytes
    tx += struct.pack("<Q", nonce)
    tx += _borsh_string(receiver_id)
    tx += block_hash
    tx += struct.pack("<I", 1)  # exactly one action
    tx += action
    return tx


def _failure_reason(status: dict) -> str:
    try:
        failure = status.get("Failure") or {}
        action = failure.get("ActionError") or {}
        kind = action.get("kind") or {}
        for key in ("FunctionCallError", "AccountDoesNotExist", "LackBalanceForState", "ActionError"):
            if key in kind:
                return f"{key}: {json.dumps(kind[key])[:400]}"
        return json.dumps(failure)[:400]
    except Exception:  # noqa: BLE001
        return "transaction failed"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _send_transaction(*, signer: str, private_key: str, receiver: str, action: bytes) -> dict:
    """Sign one action with the given key and broadcast it.

    Returns {"success": bool, "tx_hash": str, "error": str|None}.
    """
    key, public = _parse_key(private_key)
    public_key_str = "ed25519:" + b58encode(public)

    access = _rpc(
        "query",
        {
            "request_type": "view_access_key",
            "finality": "optimistic",
            "account_id": signer,
            "public_key": public_key_str,
        },
        timeout=20,
    )
    access_result = access.get("result") or {}
    if "error" in access:
        raise RuntimeError(f"cannot read access key: {access['error'].get('cause', access['error'])}")
    if isinstance(access_result, dict) and access_result.get("error"):
        raise RuntimeError(f"cannot read access key: {access_result['error']}")
    nonce = int(access_result["nonce"]) + 1

    block = _rpc("block", {"finality": "final"}, timeout=20)
    if "error" in block:
        raise RuntimeError(f"cannot read block: {block['error'].get('cause', block['error'])}")
    block_hash = b58decode(block["result"]["header"]["hash"])
    if len(block_hash) != 32:
        raise RuntimeError("bad block hash from RPC")

    tx = _build_transaction(
        signer_id=signer,
        public_key=public,
        nonce=nonce,
        receiver_id=receiver,
        block_hash=block_hash,
        action=action,
    )
    # NEAR signatures are over SHA-256 of the borsh-encoded transaction
    # (near-crypto `Signature::verify` is called with `signed_tx.get_hash()`).
    digest = hashlib.sha256(tx).digest()
    signature = key.sign(digest)
    signed = tx + b"\x00" + signature

    result = _rpc("broadcast_tx_commit", [base64.b64encode(signed).decode()], timeout=40)
    if "error" in result:
        detail = result["error"]
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        data = detail.get("data") if isinstance(detail, dict) else None
        if data:
            message = f"{message}: {json.dumps(data)[:500]}"
        raise RuntimeError(f"RPC rejected transaction: {message}")
    payload = result["result"]
    status = payload.get("status") or {}
    tx_hash = (payload.get("transaction") or {}).get("hash", "")
    success = "SuccessValue" in status or "SuccessReceiptId" in status
    return {
        "success": success,
        "tx_hash": tx_hash,
        "error": None if success else _failure_reason(status),
    }


def sign_and_send(
    *,
    method_name: str,
    args: dict,
    receiver_id: str | None = None,
    gas: int = GAS_FUNCTION_CALL,
    deposit: int = 1,
) -> dict:
    """Sign one FunctionCall with the co-pilot agent key and broadcast it."""
    if not agent_configured():
        raise RuntimeError("co-pilot agent wallet is not configured on this deployment")
    signer = settings.AI_AGENT_ACCOUNT
    receiver = receiver_id or signer
    action = _function_call_action(method_name, args, gas, deposit)
    return _send_transaction(signer=signer, private_key=settings.AI_AGENT_PRIVATE_KEY, receiver=receiver, action=action)


def send_near_from(*, signer: str, private_key: str, receiver: str, amount_yocto: int) -> dict:
    """Plain NEAR transfer signed by an explicit key (ops / treasury demos)."""
    return _send_transaction(
        signer=signer, private_key=private_key, receiver=receiver, action=_transfer_action(amount_yocto)
    )


def transfer_from_allowance(*, owner: str, receiver: str, amount: int, memo: str | None = None) -> dict:
    """Pay `receiver` `amount` yoctoNEAR from `owner`'s co-pilot budget.

    Kept under the historical function name so API call sites stay stable;
    this now calls the native NEAR budget contract's agent-only `pay` method.
    The memo remains in the off-chain PaymentIntent audit record.
    """
    return sign_and_send(
        method_name="pay",
        args={"owner": owner, "receiver": receiver, "amount": str(amount)},
        receiver_id=settings.AI_BUDGET_CONTRACT,
        gas=GAS_FUNCTION_CALL,
        deposit=0,
    )
