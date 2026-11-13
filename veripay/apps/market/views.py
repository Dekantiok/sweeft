from __future__ import annotations

import json

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.escrow import near
from apps.escrow.models import EscrowAgreement
from apps.logistics.models import LogisticsPartner
from apps.wallets.views import current_wallet

from .models import Listing, PurchaseRequest


def _reputation(account_id: str) -> dict:
    try:
        return near.view_call(settings.VERIPAY_CONTRACT, "reputation_of", {"account_id": account_id}) or {}
    except Exception:  # noqa: BLE001 - page still renders without chain
        return {}


def _reputations(account_ids: list[str]) -> dict[str, dict]:
    """One batch call for list pages."""
    if not account_ids:
        return {}
    try:
        rows = near.view_call(settings.VERIPAY_CONTRACT, "reputations", {"account_ids": account_ids}) or []
        return dict(zip(account_ids, rows))
    except Exception:  # noqa: BLE001
        return {}


def _active_providers():
    return list(LogisticsPartner.objects.filter(status="ACTIVE"))


def market_browse(request):
    q = (request.GET.get("q") or "").strip()
    kind = (request.GET.get("kind") or "").strip()
    listings = Listing.objects.filter(status="active")
    if q:
        listings = listings.filter(title__icontains=q) | listings.filter(description__icontains=q) | listings.filter(
            category__icontains=q
        )
    if kind in ("goods", "service"):
        listings = listings.filter(kind=kind)
    return render(
        request,
        "market.html",
        {"listings": listings.distinct()[:60], "q": q, "kind": kind, "wallet_account": current_wallet(request)},
    )


def listing_detail(request, slug: str):
    listing = get_object_or_404(Listing, slug=slug)
    wallet = current_wallet(request)
    rep = _reputation(listing.seller_account)
    my_request = None
    if wallet:
        my_request = listing.requests.filter(buyer_account=wallet).exclude(status="declined").first()
    provider_name = ""
    if listing.logistics_provider:
        partner = LogisticsPartner.objects.filter(settlement_wallet=listing.logistics_provider).first()
        provider_name = partner.company_name if partner else "Unknown provider"
    return render(
        request,
        "listing_detail.html",
        {
            "listing": listing,
            "seller_rep": rep,
            "provider_name": provider_name,
            "my_request": my_request,
            "is_owner": wallet == listing.seller_account,
            "wallet_account": wallet,
            "fmt": near.format_near_amount,
        },
    )


def listing_new(request):
    wallet = current_wallet(request)
    if not wallet:
        return redirect("/?connect=1")
    if request.method == "POST":
        price = near.parse_near_amount(request.POST.get("price_amount", "0"))
        if price <= 0:
            messages.error(request, "Price must be greater than zero.")
            return redirect("listing-new")
        provider = request.POST.get("logistics_provider", "").strip()
        listing = Listing.objects.create(
            seller_account=wallet,
            title=request.POST.get("title", "").strip(),
            kind=request.POST.get("kind", "goods"),
            category=request.POST.get("category", "").strip(),
            description=request.POST.get("description", "").strip(),
            price_amount=str(price),
            location=request.POST.get("location", "").strip(),
            photos=[u.strip() for u in request.POST.get("photos", "").splitlines() if u.strip()],
            dispatch_days=int(request.POST.get("dispatch_days") or 7),
            inspection_days=int(request.POST.get("inspection_days") or 2),
            logistics_note=request.POST.get("logistics_note", "").strip(),
            logistics_provider=provider,
        )
        messages.success(request, f"Listing “{listing.title}” published.")
        return redirect("listing-detail", slug=listing.slug)
    return render(request, "listing_new.html", {"wallet_account": wallet, "providers": _active_providers()})


def listing_mine(request):
    wallet = current_wallet(request)
    if not wallet:
        return redirect("/?connect=1")
    listings = Listing.objects.filter(seller_account=wallet)
    raw_requests = list(
        PurchaseRequest.objects.filter(listing__in=listings).select_related("listing").order_by("-created_at")
    )
    # Buyer trust record, one batch contract call for the whole page.
    reps = _reputations(sorted({r.buyer_account for r in raw_requests if r.status == "pending"}))
    requests = [
        {"req": r, "rep": reps.get(r.buyer_account) or {}, "show": r.status == "pending"}
        for r in raw_requests
    ]
    return render(
        request,
        "listing_mine.html",
        {
            "listings": listings,
            "requests": requests,
            "wallet_account": wallet,
            "fmt": near.format_near_amount,
        },
    )


@csrf_exempt
@require_POST
def listing_status(request, slug: str):
    wallet = current_wallet(request)
    listing = get_object_or_404(Listing, slug=slug)
    if not wallet or wallet != listing.seller_account:
        return JsonResponse({"ok": False, "error": "not your listing"}, status=403)
    body = json.loads(request.body or "{}")
    new_status = body.get("status", "")
    if new_status not in dict(Listing.STATUS_CHOICES):
        return JsonResponse({"ok": False, "error": "invalid status"}, status=400)
    listing.status = new_status
    listing.save(update_fields=["status", "updated_at"])
    return JsonResponse({"ok": True, "status": listing.status})


@csrf_exempt
@require_POST
def purchase_request(request, slug: str):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    listing = get_object_or_404(Listing, slug=slug)
    if wallet == listing.seller_account:
        return JsonResponse({"ok": False, "error": "this is your own listing"}, status=400)
    body = json.loads(request.body or "{}")
    req, created = PurchaseRequest.objects.get_or_create(
        listing=listing,
        buyer_account=wallet,
        status="pending",
        defaults={"message": body.get("message", "")[:1000]},
    )
    return JsonResponse({"ok": True, "request_id": req.id, "created": created})


@csrf_exempt
@require_POST
def purchase_request_decline(request, request_id: int):
    wallet = current_wallet(request)
    req = get_object_or_404(PurchaseRequest, pk=request_id)
    if not wallet or wallet != req.listing.seller_account:
        return JsonResponse({"ok": False, "error": "not your listing"}, status=403)
    req.status = "declined"
    req.save(update_fields=["status"])
    return JsonResponse({"ok": True})


def profile(request, account_id: str):
    """Public trust page: on-chain reputation + listings + dealings."""
    rep = _reputation(account_id)
    listings = Listing.objects.filter(seller_account=account_id, status="active")[:24]
    try:
        escrows = near.escrows_for(account_id)
    except Exception:  # noqa: BLE001
        escrows = []
    completed = [e for e in escrows if e.get("state") == "Completed"]
    return render(
        request,
        "profile.html",
        {
            "profile_account": account_id,
            "rep": rep,
            "volume_display": near.format_near_amount((rep or {}).get("volume") or 0),
            "listings": listings,
            "completed_count": len(completed),
            "escrow_count": len(escrows),
            "wallet_account": current_wallet(request),
            "fmt": near.format_near_amount,
            "explorer_tx": near.settings.EXPLORER_URL,
        },
    )


def listing_escrow_prefill(request, slug: str):
    """Seller flow: continue from a purchase request into escrow creation."""
    wallet = current_wallet(request)
    if not wallet:
        return redirect("/?connect=1")
    listing = get_object_or_404(Listing, slug=slug)
    if wallet != listing.seller_account:
        return redirect("listing-detail", slug=slug)
    buyer = (request.GET.get("buyer") or "").strip()
    return redirect(f"/escrow/new/?listing={listing.pk}&buyer={buyer}")
