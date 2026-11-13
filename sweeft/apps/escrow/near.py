"""Minimal NEAR JSON-RPC client (read-only views, transaction receipts).

The Django backend never signs financial transactions: user wallets sign
`create_escrow`, `accept_escrow`, funding, settlement, etc. This module exists
to read contract state for verification, reconciliation and projections.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from django.conf import settings

NATIVE_NEAR_DECIMALS = 24


def _rpc(method: str, params: dict, timeout: int = 25):
    payload = json.dumps({"jsonrpc": "2.0", "id": "sweeft", "method": method, "params": params}).encode()
    req = urllib.request.Request(
        settings.NEAR_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:  # RPC errors still carry JSON bodies
        return json.loads(e.read())


def view_call(contract: str, method: str, args: dict):
    """Call a read-only contract method; returns the parsed JSON value."""
    if not contract:
        raise RuntimeError("contract account is not configured")
    result = _rpc(
        "query",
        {
            "request_type": "call_function",
            "finality": "final",
            "account_id": contract,
            "method_name": method,
            "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
        },
    )
    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error'].get('cause', result['error'])}")
    data = bytes(result["result"].get("result", []))
    return json.loads(data.decode()) if data else None


def get_escrow(escrow_id: str):
    return view_call(settings.SWEEFT_CONTRACT, "get_escrow", {"escrow_id": escrow_id})


# Page renders may repeat reads within seconds; a tiny TTL cache keeps
# navigation snappy when the RPC is slow. The sync/refresh endpoints use the
# raw `get_escrow` above so "Refresh from chain" is always fresh.
_escrow_cache: dict = {}


def get_escrow_cached(escrow_id: str, ttl_seconds: int = 8):
    import time

    now = time.time()
    hit = _escrow_cache.get(escrow_id)
    if hit and now - hit[1] < ttl_seconds:
        return hit[0]
    value = get_escrow(escrow_id)
    _escrow_cache[escrow_id] = (value, now)
    return value


def escrows_for(account_id: str):
    return view_call(settings.SWEEFT_CONTRACT, "escrows_for", {"account_id": account_id}) or []


def native_balance(account_id: str) -> str:
    """Native NEAR balance in yoctoNEAR (no token contract needed)."""
    result = _rpc(
        "query",
        {"request_type": "view_account", "finality": "final", "account_id": account_id},
        timeout=15,
    )
    if "error" in result:
        return "0"
    return str((result.get("result") or {}).get("amount") or 0)


# Platform config (owner, fee bps, outlayer addresses) changes rarely; cache it
# briefly so page loads don't pay two RPC round-trips for every render.
_config_cache: dict = {"value": None, "ts": 0.0}


def get_config_cached(ttl_seconds: int = 30) -> dict:
    import time

    now = time.time()
    if _config_cache["value"] is not None and now - _config_cache["ts"] < ttl_seconds:
        return _config_cache["value"]
    value = view_call(settings.SWEEFT_CONTRACT, "get_config", {}) or {}
    _config_cache["value"] = value
    _config_cache["ts"] = now
    return value


# ---------------------------------------------------------------------------
# Amount formatting and parsing (native NEAR, 24 decimals).
# ---------------------------------------------------------------------------

NATIVE_NEAR_DECIMALS = 24


def _format_units(amount_yocto: str | int, decimals: int) -> str:
    try:
        value = int(amount_yocto or 0)
    except (TypeError, ValueError):
        return str(amount_yocto)
    unit = 10**decimals
    whole, frac = divmod(value, unit)
    if frac == 0:
        return f"{whole:,}"
    frac_text = f"{frac:0{decimals}d}".rstrip("0")
    return f"{whole:,}.{frac_text}"


def _parse_units(text: str, decimals: int) -> int:
    text = (text or "0").strip().replace(",", "")
    if not text:
        return 0
    whole, _, frac = text.partition(".")
    frac = (frac + "0" * decimals)[:decimals]
    return int(whole or "0") * (10**decimals) + int(frac or "0")


def format_near_amount(amount: str | int) -> str:
    """Compact native NEAR display (up to 6 decimals, zeros trimmed)."""
    return _format_units(amount, NATIVE_NEAR_DECIMALS)


def parse_near_amount(text: str) -> int:
    """Parse a human NEAR amount (e.g. '0.115') into yoctoNEAR."""
    return _parse_units(text, NATIVE_NEAR_DECIMALS)
