"""Dispute workflow API.

Contract events (report_problem / open_dispute) lock the funds on-chain; these
endpoints record the human case file — reason, evidence from both sides, the
AI advisory, and the platform resolution. No endpoint here can move funds:
resolution requires the owner's on-chain `resolve_review` transaction first.
"""
from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.ai import services as ai_services
from apps.ai.providers.base import AIProviderError
from apps.wallets.views import current_wallet

from . import near
from .models import Dispute, DisputeEvidence

REVIEW_STATES = {"ReviewRequired", "Completed", "Refunded"}


def _wallet_required(view):
    def wrapped(request, *args, **kwargs):
        wallet = current_wallet(request)
        if not wallet:
            return JsonResponse({"ok": False, "error": "wallet not authenticated"}, status=401)
        request.wallet = wallet
        return view(request, *args, **kwargs)

    return wrapped


def _owner_account() -> str | None:
    try:
        return (near.get_config_cached() or {}).get("owner")
    except Exception:  # noqa: BLE001
        return None


def _side_of(chain: dict, wallet: str) -> str | None:
    if chain.get("buyer") == wallet:
        return "buyer"
    if chain.get("seller") == wallet:
        return "seller"
    return None


def _close_dispute_if_terminal(dispute: Dispute, chain: dict | None) -> Dispute:
    """Close solved case files when the contract leaves review."""
    if dispute.status == "open" and chain and chain.get("state") != "ReviewRequired":
        dispute.status = "resolved"
        dispute.resolved_at = dispute.resolved_at or timezone.now()
        dispute.save(update_fields=["status", "resolved_at"])
    return dispute


def _dispute_json(d: Dispute) -> dict:
    return {
        "id": d.id,
        "escrow_id": d.escrow_id,
        "opened_by": d.opened_by,
        "side": d.side,
        "reason": d.reason,
        "status": d.status,
        "ai_recommendation": d.ai_recommendation,
        "resolution_pct": d.resolution_pct,
        "resolution_note": d.resolution_note,
        "resolution_tx": d.resolution_tx,
        "opened_tx": d.opened_tx,
        "created_at": d.created_at.isoformat(),
        "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
        "evidence": [
            {"author": e.author, "side": e.side, "text": e.text, "links": e.links, "created_at": e.created_at.isoformat()}
            for e in d.evidence.all()
        ],
    }


@require_POST
@_wallet_required
def api_open_dispute(request):
    """Record the case file after the on-chain dispute transaction."""
    body = json.loads(request.body or "{}")
    escrow_id = (body.get("escrow_id") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not escrow_id or not reason:
        return JsonResponse({"ok": False, "error": "escrow_id and reason are required"}, status=400)

    chain = near.get_escrow(escrow_id)
    if not chain:
        return JsonResponse({"ok": False, "error": "escrow not found on-chain"}, status=404)
    side = _side_of(chain, request.wallet)
    if not side:
        return JsonResponse({"ok": False, "error": "only the buyer or seller can open a dispute"}, status=403)
    if chain["state"] != "ReviewRequired":
        return JsonResponse(
            {"ok": False, "error": f"on-chain state is {chain['state']} — open the dispute on-chain first"},
            status=400,
        )

    dispute, created = Dispute.objects.get_or_create(
        escrow_id=escrow_id,
        status="open",
        defaults={
            "opened_by": request.wallet,
            "side": side,
            "reason": reason[:4000],
            "reason_hash": hashlib.sha256(reason.encode()).hexdigest(),
            "opened_tx": body.get("tx_hash", ""),
        },
    )
    return JsonResponse({"ok": True, "created": created, "dispute": _dispute_json(dispute)})


@require_POST
@_wallet_required
def api_add_evidence(request, dispute_id: int):
    dispute = Dispute.objects.filter(pk=dispute_id).first()
    if not dispute:
        return JsonResponse({"ok": False, "error": "unknown dispute"}, status=404)
    chain = near.get_escrow(dispute.escrow_id)
    _close_dispute_if_terminal(dispute, chain)
    if dispute.status != "open":
        return JsonResponse({"ok": False, "error": "dispute is already resolved"}, status=400)
    side = _side_of(chain or {}, request.wallet)
    if not side:
        return JsonResponse({"ok": False, "error": "only the buyer or seller can file evidence"}, status=403)

    body = json.loads(request.body or "{}")
    text = (body.get("text") or "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "text is required"}, status=400)
    links = [str(u).strip() for u in (body.get("links") or []) if str(u).strip()][:10]
    ev = DisputeEvidence.objects.create(dispute=dispute, author=request.wallet, side=side, text=text[:4000], links=links)
    return JsonResponse({"ok": True, "evidence_id": ev.id})


@require_POST
@_wallet_required
def api_ai_review(request, dispute_id: int):
    """Ask the configured AI provider for an advisory split (never binding)."""
    dispute = Dispute.objects.filter(pk=dispute_id).first()
    if not dispute:
        return JsonResponse({"ok": False, "error": "unknown dispute"}, status=404)
    chain = near.get_escrow(dispute.escrow_id) or {}
    side = _side_of(chain, request.wallet)
    owner = _owner_account()
    if not side and request.wallet != owner:
        return JsonResponse({"ok": False, "error": "only the parties or the platform owner can request a review"}, status=403)

    corpus = [f"[{dispute.side} opening] {dispute.reason}"]
    for e in dispute.evidence.all():
        corpus.append(f"[{e.side} evidence] {e.text}" + (f" links: {', '.join(e.links)}" if e.links else ""))
    terms = chain.get("terms_hash") or ""
    try:
        rec = ai_services.recommend_dispute_split("\n\n".join(corpus), terms)
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)

    dispute.ai_recommendation = rec
    dispute.save(update_fields=["ai_recommendation"])
    return JsonResponse({"ok": True, "recommendation": rec})


@require_POST
@_wallet_required
def api_resolve_dispute(request, dispute_id: int):
    """Record the review-board resolution after on-chain resolve_review."""
    dispute = Dispute.objects.filter(pk=dispute_id).first()
    if not dispute:
        return JsonResponse({"ok": False, "error": "unknown dispute"}, status=404)
    chain = near.get_escrow(dispute.escrow_id)
    if not _side_of(chain or {}, request.wallet):
        return JsonResponse({"ok": False, "error": "only the buyer or seller can record a resolution"}, status=403)
    if not chain:
        return JsonResponse({"ok": False, "error": "escrow not found on-chain"}, status=404)
    if chain["state"] in ("ReviewRequired",):
        return JsonResponse({"ok": False, "error": "resolve on-chain first (resolve_review)"}, status=400)

    body = json.loads(request.body or "{}")
    pct = body.get("release_pct")
    dispute.status = "resolved"
    dispute.resolution_pct = int(pct) if pct is not None else None
    dispute.resolution_note = (body.get("note") or "")[:2000]
    dispute.resolution_tx = body.get("tx_hash", "")
    dispute.resolved_at = timezone.now()
    dispute.save()
    return JsonResponse({"ok": True, "dispute": _dispute_json(dispute)})


@require_GET
def api_list_disputes(request):
    escrow_id = (request.GET.get("escrow_id") or "").strip()
    wallet = current_wallet(request)
    owner = _owner_account()
    qs = Dispute.objects.all()
    if escrow_id:
        qs = qs.filter(escrow_id=escrow_id)
    rows = []
    for d in qs[:50]:
        chain = near.get_escrow(d.escrow_id) or {}
        _close_dispute_if_terminal(d, chain)
        if wallet and (wallet in (chain.get("buyer"), chain.get("seller")) or wallet == owner):
            rows.append(_dispute_json(d))
        elif wallet == owner:
            rows.append(_dispute_json(d))
    return JsonResponse({"ok": True, "disputes": rows})


def disputes_console(request):
    """Platform-owner console: every open dispute with quick context."""
    wallet = current_wallet(request)
    owner = _owner_account()
    if not wallet:
        return render(request, "disputes_console.html", {"wallet_account": None, "owner": owner, "disputes": []})
    if wallet != owner:
        return render(request, "disputes_console.html", {"wallet_account": wallet, "owner": owner, "not_owner": True, "disputes": []})
    rows = []
    for d in Dispute.objects.filter(status="open")[:100]:
        chain = near.get_escrow(d.escrow_id) or {}
        rows.append({"dispute": d, "chain": chain})
    return render(request, "disputes_console.html", {"wallet_account": wallet, "owner": owner, "disputes": rows})
