from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import services
from .providers.base import AIProviderError
from .registry import provider_status


@require_GET
def ai_status(request):
    return JsonResponse({"ok": True, **provider_status()})


@csrf_exempt
@require_POST
def ai_parse(request):
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "text is required"}, status=400)
    try:
        parsed = services.parse_deal_text(text)
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, "parsed": parsed})


@csrf_exempt
@require_POST
def ai_recommend(request):
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
    evidence = (body.get("evidence") or "").strip()
    if not evidence:
        return JsonResponse({"ok": False, "error": "evidence is required"}, status=400)
    try:
        rec = services.recommend_dispute_split(evidence, body.get("terms") or "")
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, **rec})


@csrf_exempt
@require_POST
def ai_listing(request):
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
    description = (body.get("description") or "").strip()
    if not description:
        return JsonResponse({"ok": False, "error": "description is required"}, status=400)
    try:
        draft = services.draft_listing(description)
    except AIProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    return JsonResponse({"ok": True, "draft": draft})
