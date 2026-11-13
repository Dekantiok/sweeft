"""Server-side NEAR transaction pipeline (borsh encode → ed25519 sign → broadcast).

Shared by every platform-side signer: the AI co-pilot agent
(`apps.ai.agent_wallet`) and the verification relayer
(`apps.escrow.relayer`). Importing modules own their keys, policies, and
disclosures; this module only builds and submits transactions. No user keys
ever pass through here.

Trust disclosure (testnet MVP): platform keys live on the demo server because
this is a testnet build. On mainnet the same pipeline would run inside a
TEE/KMS — the on-chain permission model is the boundary either way.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.escrow.near import _rpc
from apps.wallets.base58 import b58decode, b58encode

GAS_FUNCTION_CALL = 100_000_000_000_000  # 100 Tgas


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
        raise RuntimeError("private key must be 64 bytes (ed25519: seed+public)")
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
    actions: list[bytes],
) -> bytes:
    tx = _borsh_string(signer_id)
    tx += b"\x00" + public_key  # ed25519 key type tag + 32 bytes
    tx += struct.pack("<Q", nonce)
    tx += _borsh_string(receiver_id)
    tx += block_hash
    tx += struct.pack("<I", len(actions))
    for action in actions:
        tx += action
    return tx


# ---------------------------------------------------------------------------
# Action builders (borsh tags per near-primitives Action enum)
# ---------------------------------------------------------------------------


def create_account_action() -> bytes:
    return b"\x00"  # Action::CreateAccount


def deploy_contract_action(code: bytes) -> bytes:
    return b"\x01" + _borsh_bytes(code)  # Action::DeployContract


def add_full_access_key_action(public: bytes) -> bytes:
    # Action::AddKey = 5, then PublicKey (0x00 ed25519 tag + 32 bytes),
    # then AccessKey { nonce: u64, permission: FullAccess = 1 }
    # (FunctionCall = 0, FullAccess = 1 in AccessKeyPermission).
    return b"\x05" + b"\x00" + public + struct.pack("<Q", 0) + b"\x01"


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


def send_transaction(
    *,
    signer: str,
    private_key: str,
    receiver: str,
    action: bytes | None = None,
    actions: list[bytes] | None = None,
) -> dict:
    """Sign one or more actions with the given key and broadcast them.

    Pass `action` for a single action or `actions` for a list (e.g. create
    account + transfer + add key). Returns {"success", "tx_hash", "error"}.
    """
    action_list = actions if actions is not None else [action]
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
        actions=action_list,
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


def call_function_as(
    *,
    signer: str,
    private_key: str,
    method_name: str,
    args: dict,
    receiver_id: str,
    gas: int = GAS_FUNCTION_CALL,
    deposit: int = 1,
) -> dict:
    """Sign one FunctionCall with an explicit key and broadcast it."""
    action = _function_call_action(method_name, args, gas, deposit)
    return send_transaction(signer=signer, private_key=private_key, receiver=receiver_id, action=action)


def transfer_near_as(*, signer: str, private_key: str, receiver: str, amount_yocto: int) -> dict:
    """Plain NEAR transfer signed by an explicit key (ops / treasury demos)."""
    return send_transaction(
        signer=signer, private_key=private_key, receiver=receiver, action=_transfer_action(amount_yocto)
    )
