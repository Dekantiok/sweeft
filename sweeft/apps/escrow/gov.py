"""Governance helpers: the Sputnik/Trezu DAO review board.

The escrow contract's `arbiter`/`treasury` are on-chain pointers to a DAO
account (`GOVERNANCE_DAO`). Disputes are resolved ONLY via DAO proposals:
a council member (or any party) files a `FunctionCall` proposal targeting
`resolve_review`; once the vote threshold is met the DAO executes it and the
escrow splits. This module reads that state for the UI — it never signs.
"""
from __future__ import annotations

import base64
import json

from django.conf import settings
from django.shortcuts import render

from . import near


def dao_account() -> str:
    try:
        info = near.view_call(settings.SWEEFT_CONTRACT, "get_gov_info", {}) or {}
        if info.get("arbiter"):
            return info["arbiter"]
    except Exception:  # noqa: BLE001 - settings remain the fallback
        pass
    return settings.GOVERNANCE_DAO


def fetch_policy() -> dict | None:
    try:
        return near.view_call(dao_account(), "get_policy", {}) or None
    except Exception:  # noqa: BLE001
        return None


def council_info(policy: dict | None) -> dict:
    """Council members + approval threshold derived from the DAO policy."""
    members: list[str] = []
    threshold = [1, 2]
    if policy:
        for role in policy.get("roles", []):
            if role.get("name") == "council":
                kind = role.get("kind") or {}
                if isinstance(kind, dict) and "Group" in kind:
                    members = list(kind["Group"])
        t = (policy.get("default_vote_policy") or {}).get("threshold")
        if isinstance(t, list) and len(t) == 2:
            threshold = t
    needed = 0
    if members:
        num, den = threshold[0], threshold[1]
        needed = -(-len(members) * num // den)  # ceil
    return {
        "members": members,
        "threshold": threshold,
        "votes_needed": max(needed, 1) if members else 0,
    }


def fetch_proposals(limit: int = 40) -> list[dict]:
    try:
        dao = dao_account()
        start = 0
        try:
            last = near.view_call(dao, "get_last_proposal_id", {}) or 0
            if last > limit:
                start = last - limit
        except Exception:  # noqa: BLE001 - the DAO may not expose the counter
            start = 0
        return near.view_call(dao, "get_proposals", {"from_index": start, "limit": limit}) or []
    except Exception:  # noqa: BLE001
        return []


def _decode_action_args(raw) -> dict | None:
    try:
        data = raw.get("args") if isinstance(raw, dict) else None
        if data is None:
            return None
        return json.loads(base64.b64decode(data).decode())
    except Exception:  # noqa: BLE001
        return None


def proposal_review_target(proposal: dict) -> dict | None:
    """If this proposal calls SWEEFT `resolve_review`, return its details."""
    kind = proposal.get("kind") or {}
    fc = kind.get("FunctionCall") if isinstance(kind, dict) else None
    if not fc or fc.get("receiver_id") != settings.SWEEFT_CONTRACT:
        return None
    for action in fc.get("actions", []):
        if action.get("method_name") == "resolve_review":
            args = _decode_action_args(action) or {}
            return {
                "escrow_id": args.get("escrow_id"),
                "seller_pct": args.get("seller_pct"),
            }
    return None


def proposals_for_escrow(escrow_id: str, proposals: list[dict] | None = None) -> list[dict]:
    proposals = proposals if proposals is not None else fetch_proposals()
    out = []
    for p in proposals:
        target = proposal_review_target(p)
        if target and target.get("escrow_id") == escrow_id:
            p = dict(p)
            p["review"] = target
            out.append(p)
    return out


def review_proposal_args(escrow_id: str, seller_pct: int) -> dict:
    """Ready-to-sign `add_proposal` args for a review-board escalation."""
    payload = json.dumps({"escrow_id": escrow_id, "seller_pct": int(seller_pct)}).encode()
    return {
        "proposal": {
            "description": f"Resolve dispute on {escrow_id}: {int(seller_pct)}% to seller",
            "kind": {
                "FunctionCall": {
                    "receiver_id": settings.SWEEFT_CONTRACT,
                    "actions": [
                        {
                            "method_name": "resolve_review",
                            "args": base64.b64encode(payload).decode(),
                            "deposit": "0",
                            "gas": 100_000_000_000_000,
                        }
                    ],
                }
            },
        }
    }


def proposal_bond_yocto(policy: dict | None) -> int:
    try:
        return int((policy or {}).get("proposal_bond") or 0)
    except (TypeError, ValueError):
        return 0


def summary() -> dict:
    """Compact governance snapshot for consoles."""
    policy = fetch_policy()
    council = council_info(policy)
    proposals = []
    for p in fetch_proposals():
        p = dict(p)
        p["review"] = proposal_review_target(p)
        proposals.append(p)
    active = [p for p in proposals if p.get("status") == "InProgress"]
    dao = dao_account()
    treasury = {
        "near": 0,
    }
    try:
        account = near._rpc("query", {"request_type": "view_account", "finality": "final", "account_id": dao}, timeout=15)
        treasury["near"] = int(((account.get("result") or {}).get("amount")) or 0)
    except Exception:  # noqa: BLE001
        pass
    gov = {}
    try:
        gov = near.view_call(settings.SWEEFT_CONTRACT, "get_gov_info", {}) or {}
    except Exception:  # noqa: BLE001
        pass
    return {
        "dao": dao,
        "council": council,
        "policy": policy,
        "proposal_bond_display": near.format_near_amount(proposal_bond_yocto(policy)),
        "proposal_bond_yocto": str(proposal_bond_yocto(policy)),
        "proposals": proposals,
        "active_count": len(active),
        "treasury_near_display": _fmt_near_round(treasury["near"]),
        "gov": gov,
    }


def _fmt_near_round(yocto: int) -> str:
    """Compact NEAR display (4 decimals, trailing zeros trimmed)."""
    text = f"{yocto / 10**24:.4f}".rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def gov_console(request):
    """`/governance/` — review board & treasury console (reads chain state)."""
    wallet = request.session.get("wallet")
    data = summary()
    members = data["council"]["members"] or []
    return render(
        request,
        "governance.html",
        {
            "wallet_account": wallet,
            "is_council": bool(wallet and wallet in members),
            "member_votes_map": {
                p.get("id"): p.get("votes") or {}
                for p in data["proposals"]
            },
            **data,
        },
    )

