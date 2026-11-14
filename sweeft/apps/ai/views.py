from __future__ import annotations

import json

from django.conf import settings
from apps.escrow.http import read_json
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.escrow import near as near_mod
from apps.wallets.views import current_wallet

from . import services, tools
from .agent import run_agent
from .agent_wallet import agent_configured, transfer_from_allowance
from .models import PaymentIntent
from .providers.base import AIProviderError
from .registry import provider_status


@require_GET
def ai_status(request):
    return JsonResponse({"ok": True, **provider_status()})


@require_GET
def ai_agent_info(request):
    """Co-pilot payment wallet status for the connected wallet (budget + caps)."""
    wallet = current_wallet(request)
    allowance = tools.allowance_of(wallet) if wallet else 0
    return JsonResponse(
        {
            "ok": True,
            "agent_account": tools._onchain_agent(),
            "configured": agent_configured() and tools._onchain_agent() == settings.AI_AGENT_ACCOUNT,
            "enabled": bool(settings.AI_AGENT_ENABLED),
            "wallet": wallet,
            "allowance": str(allowance),
            "allowance_display": near_mod.format_near_amount(allowance),
            "max_payment_display": near_mod.format_near_amount(tools._onchain_max_payment()),
            "grant_amount": str(int(settings.AI_AGENT_GRANT)),
            "grant_display": near_mod.format_near_amount(int(settings.AI_AGENT_GRANT)),
            "budget_contract": settings.AI_BUDGET_CONTRACT,
        }
    )


@require_POST
def ai_payment_execute(request, intent_id: int):
    """Execute a proposed payment from the user's granted co-pilot budget.

    The agent key signs `ft_transfer_from`; the token contract enforces the
    on-chain allowance. The user's session must own the intent.
    """
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    intent = PaymentIntent.objects.filter(id=intent_id, wallet=wallet).first()
    if intent is None:
        return JsonResponse({"ok": False, "error": "payment intent not found for this wallet"}, status=404)
    if intent.status == PaymentIntent.Status.EXECUTED:
        return JsonResponse(
            {
                "ok": True,
                "status": "executed",
                "tx_hash": intent.tx_hash,
                "explorer": f"{settings.EXPLORER_URL}/txns/{intent.tx_hash}",
            }
        )
    units = int(intent.amount)
    from .tools import _onchain_agent, _onchain_max_payment

    if units > int(settings.AI_AGENT_MAX_PAYMENT) or units > _onchain_max_payment():
        return JsonResponse({"ok": False, "error": "above the co-pilot per-payment limit"}, status=400)
    if _onchain_agent() != settings.AI_AGENT_ACCOUNT:
        return JsonResponse({"ok": False, "error": "co-pilot agent differs from the on-chain budget agent"}, status=503)
    if not agent_configured():
        return JsonResponse({"ok": False, "error": "co-pilot wallet is not configured on this deployment"}, status=503)

    from django.db import transaction

    # Claim the intent before signing so double-clicks cannot execute it twice.
    with transaction.atomic():
        claimed = PaymentIntent.objects.select_for_update().filter(id=intent.id, status=PaymentIntent.Status.PROPOSED).update(status=PaymentIntent.Status.PROCESSING)
        if claimed != 1:
            return JsonResponse({"ok": False, "error": "payment is already being executed"}, status=409)
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=["status"])

    try:
        result = transfer_from_allowance(
            owner=wallet,
            receiver=intent.to_account,
            amount=units,
            memo=intent.memo or None,
        )
    except Exception as e:  # noqa: BLE001 — surface RPC/signing errors plainly
        intent.status = PaymentIntent.Status.FAILED
        intent.error = str(e)[:500]
        intent.save(update_fields=["status", "error"])
        return JsonResponse({"ok": False, "error": str(e)}, status=502)

    if not result["success"]:
        intent.status = PaymentIntent.Status.FAILED
        intent.error = (result.get("error") or "transaction failed")[:500]
        intent.tx_hash = result.get("tx_hash") or ""
        intent.save(update_fields=["status", "error", "tx_hash"])
        return JsonResponse({"ok": False, "error": intent.error, "tx_hash": intent.tx_hash}, status=502)

    intent.status = PaymentIntent.Status.EXECUTED
    intent.tx_hash = result["tx_hash"]
    intent.executed_at = timezone.now()
    intent.save(update_fields=["status", "tx_hash", "executed_at"])
    return JsonResponse(
        {
            "ok": True,
            "status": "executed",
            "tx_hash": intent.tx_hash,
            "explorer": f"{settings.EXPLORER_URL}/txns/{intent.tx_hash}",
        }
    )


def _wallet_required(view):
    def wrapped(request, *args, **kwargs):
        if not current_wallet(request):
            return JsonResponse({"ok": False, "error": "connect your wallet to use the trade co-pilot"}, status=401)
        return view(request, *args, **kwargs)

    return wrapped


@require_POST
@_wallet_required
def ai_parse(request):
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "text is required"}, status=400)
    try:
        parsed = services.parse_deal_text(text)
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, "parsed": parsed})


@require_POST
@_wallet_required
def ai_recommend(request):
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    evidence = (body.get("evidence") or "").strip()
    if not evidence:
        return JsonResponse({"ok": False, "error": "evidence is required"}, status=400)
    try:
        rec = services.recommend_dispute_split(evidence, body.get("terms") or "")
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, **rec})


@require_POST
@_wallet_required
def ai_listing(request):
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    description = (body.get("description") or "").strip()
    if not description:
        return JsonResponse({"ok": False, "error": "description is required"}, status=400)
    try:
        draft = services.draft_listing(description)
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, "draft": draft})


@require_POST
def ai_chat(request):
    """One co-pilot turn: natural language -> validated tools -> reply + action cards.

    The agent proposes; the wallet authorizes. No endpoint here signs anything.
    """
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet to use the trade co-pilot"}, status=401)
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    message = (body.get("message") or "").strip()
    if not message:
        return JsonResponse({"ok": False, "error": "message is required"}, status=400)
    try:
        result = run_agent(message[:2000], wallet)
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    result["wallet"] = wallet
    return JsonResponse({"ok": True, **result})
