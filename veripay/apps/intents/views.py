from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import services
from .providers.base import IntentsError
from .registry import get_intents_provider, intents_status


@require_GET
def intents_status_view(request):
    return JsonResponse({"ok": True, **intents_status()})


@csrf_exempt
@require_POST
def intents_quote(request):
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
    try:
        if body.get("escrow_id"):
            result = services.quote_for_escrow(
                body["escrow_id"],
                asset_out=body.get("asset_out") or "nep141:tusd",
                asset_in=body.get("asset_in") or "eth:usdc",
            )
        else:
            asset_out = body.get("asset_out")
            amount_out = body.get("amount_out")
            if not asset_out or not amount_out:
                return JsonResponse({"ok": False, "error": "escrow_id or (asset_out, amount_out) required"}, status=400)
            quote = get_intents_provider().quote(
                asset_out=asset_out, amount_out=str(amount_out), asset_in=body.get("asset_in") or "eth:usdc"
            )
            result = {
                "asset_out": quote.asset_out,
                "amount_out": quote.amount_out,
                "asset_in": quote.asset_in,
                "amount_in": quote.amount_in,
                "deposit_address": quote.deposit_address,
                "quote_id": quote.quote_id,
                "provider": quote.provider,
                "dry_run": quote.dry_run,
            }
    except IntentsError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, "quote": result})


@require_GET
def intents_quote_status(request, quote_id: str):
    try:
        status = get_intents_provider().status(quote_id)
    except IntentsError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, "status": status})
