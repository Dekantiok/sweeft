from __future__ import annotations

import json

from django.conf import settings
from django.contrib import messages
from apps.escrow.http import read_json
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.escrow import near
from apps.escrow.models import EscrowAgreement
from apps.logistics.models import LogisticsPartner
from apps.wallets.views import current_wallet

from .models import Listing, Offer, PurchaseRequest


def _reputation(account_id: str) -> dict:
    try:
        return near.view_call(settings.SWEEFT_CONTRACT, "reputation_of", {"account_id": account_id}) or {}
    except Exception:  # noqa: BLE001 - page still renders without chain
        return {}


def _reputations(account_ids: list[str]) -> dict[str, dict]:
    """One batch call for list pages."""
    if not account_ids:
        return {}
    try:
        rows = near.view_call(settings.SWEEFT_CONTRACT, "reputations", {"account_ids": account_ids}) or []
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
    my_offer = None
    owner_requests = []
    if wallet:
        my_request = listing.requests.filter(buyer_account=wallet).exclude(status="declined").first()
        if wallet == listing.seller_account:
            owner_requests = list(
                listing.requests.filter(status="pending").order_by("-created_at")[:10]
            )
        else:
            my_offer = listing.offers.filter(buyer_account=wallet).order_by("-updated_at").first()
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
            "my_offer": my_offer,
            "my_offer_open": bool(my_offer and my_offer.is_open),
            "is_owner": wallet == listing.seller_account,
            "owner_requests": owner_requests,
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
    # Offers are negotiated per (listing, buyer); attach them to the matching
    # request row, and surface open offers that have no pending request yet.
    offers = list(Offer.objects.filter(listing__in=listings).select_related("listing").order_by("-updated_at"))
    offers_by_key: dict = {}
    for offer in offers:
        offers_by_key.setdefault((offer.listing_id, offer.buyer_account), []).append(offer)
    pending_keys = {(r["req"].listing_id, r["req"].buyer_account) for r in requests if r["req"].status == "pending"}
    for item in requests:
        item["offers"] = offers_by_key.get((item["req"].listing_id, item["req"].buyer_account), [])
    open_offers = [
        offer
        for offer in offers
        if offer.status in Offer.OPEN_STATUSES and (offer.listing_id, offer.buyer_account) not in pending_keys
    ]
    return render(
        request,
        "listing_mine.html",
        {
            "listings": listings,
            "requests": requests,
            "open_offers": open_offers,
            "wallet_account": wallet,
            "fmt": near.format_near_amount,
        },
    )


@require_POST
def listing_status(request, slug: str):
    wallet = current_wallet(request)
    listing = get_object_or_404(Listing, slug=slug)
    if not wallet or wallet != listing.seller_account:
        return JsonResponse({"ok": False, "error": "not your listing"}, status=403)
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    new_status = body.get("status", "")
    if new_status not in dict(Listing.STATUS_CHOICES):
        return JsonResponse({"ok": False, "error": "invalid status"}, status=400)
    listing.status = new_status
    listing.save(update_fields=["status", "updated_at"])
    return JsonResponse({"ok": True, "status": listing.status})


@require_POST
def purchase_request(request, slug: str):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    listing = get_object_or_404(Listing, slug=slug)
    if listing.status != "active":
        return JsonResponse({"ok": False, "error": "this listing is no longer active"}, status=400)
    if wallet == listing.seller_account:
        return JsonResponse({"ok": False, "error": "this is your own listing"}, status=400)
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    req, created = PurchaseRequest.objects.get_or_create(
        listing=listing,
        buyer_account=wallet,
        status="pending",
        defaults={"message": body.get("message", "")[:1000]},
    )
    return JsonResponse({"ok": True, "request_id": req.id, "created": created})


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
    offer_id = (request.GET.get("offer") or "").strip()
    if offer_id.isdigit():
        offer = Offer.objects.filter(
            pk=offer_id, listing=listing, seller_account=wallet, status="accepted"
        ).first()
        if offer:
            return redirect(f"/escrow/new/?listing={listing.slug}&buyer={offer.buyer_account}&offer={offer.id}")
    return redirect(f"/escrow/new/?listing={listing.pk}&buyer={buyer}")


# ---------------------------------------------------------------------------
# Offers API — session-wallet (CSRF-protected) negotiation endpoints.
#
# Amounts are yoctoNEAR strings (the Listing.price_amount convention); plain
# decimal NEAR text (e.g. "1.25") is also accepted for convenience.
# ---------------------------------------------------------------------------


def _parse_offer_amount(value, *, decimal: bool | None = None) -> int:
    """Parse an amount as yoctoNEAR digits (Listing.price_amount convention).

    Plain decimal NEAR text (e.g. "1.25") is also accepted: a value containing
    "." is parsed as decimal NEAR, unless the caller forces a convention.
    """
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text:
        return 0
    try:
        as_decimal = ("." in text) if decimal is None else decimal
        return near.parse_near_amount(text) if as_decimal else int(text)
    except (TypeError, ValueError):
        return 0


def _offer_card(offer: Offer) -> dict:
    return {
        "offer_id": offer.id,
        "listing": offer.listing.title,
        "listing_slug": offer.listing.slug,
        "buyer": offer.buyer_account,
        "seller": offer.seller_account,
        "amount": offer.amount,
        "amount_display": offer.amount_display,
        "max_amount": offer.max_amount,
        "max_display": offer.max_display,
        "status": offer.status,
        "message": offer.message,
        "history": offer.history,
        "created_at": offer.created_at.isoformat(),
        "updated_at": offer.updated_at.isoformat(),
    }


def _offer_parties(offer_id: int, wallet: str, side: str):
    """Load an offer and enforce that ``wallet`` is ``side`` of it."""
    offer = Offer.objects.filter(pk=offer_id).select_related("listing").first()
    if offer is None:
        return None, JsonResponse({"ok": False, "error": "offer not found"}, status=404)
    if side == "buyer" and offer.buyer_account != wallet:
        return None, JsonResponse({"ok": False, "error": "only the buyer can do that"}, status=403)
    if side == "seller" and offer.seller_account != wallet:
        return None, JsonResponse({"ok": False, "error": "only the seller can do that"}, status=403)
    return offer, None


def _offer_state_guard(offer: Offer, allowed: tuple):
    if offer.status not in allowed:
        return JsonResponse(
            {
                "ok": False,
                "error": f"offer is {offer.status}; this action needs {' or '.join(allowed)}",
                "status": offer.status,
            },
            status=409,
        )
    return None


@require_POST
def offer_create(request):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    listing = Listing.objects.filter(slug=(body.get("listing_slug") or "").strip()).first()
    if listing is None:
        return JsonResponse({"ok": False, "error": "listing not found"}, status=404)
    if listing.status != "active":
        return JsonResponse({"ok": False, "error": "this listing is no longer active"}, status=400)
    if wallet == listing.seller_account:
        return JsonResponse({"ok": False, "error": "this is your own listing"}, status=400)
    amount_raw = str(body.get("amount") or "").strip()
    max_raw = str(body.get("max_amount") or "").strip()
    decimal_input = "." in amount_raw or "." in max_raw
    amount = _parse_offer_amount(amount_raw, decimal=decimal_input)
    max_amount = _parse_offer_amount(max_raw, decimal=decimal_input)
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "amount must be greater than zero"}, status=400)
    if max_amount <= 0:
        return JsonResponse({"ok": False, "error": "max_amount must be greater than zero"}, status=400)
    if amount > max_amount:
        return JsonResponse({"ok": False, "error": "amount cannot exceed max_amount"}, status=400)
    existing = listing.offers.filter(buyer_account=wallet, status__in=Offer.OPEN_STATUSES).first()
    if existing is not None:
        return JsonResponse(
            {
                "ok": False,
                "error": f"you already have an open offer (#{existing.id}) on this listing",
                "offer_id": existing.id,
            },
            status=409,
        )
    message = str(body.get("message") or "")[:1000]
    offer = Offer(
        listing=listing,
        buyer_account=wallet,
        seller_account=listing.seller_account,
        amount=str(amount),
        max_amount=str(max_amount),
        message=message,
    )
    offer.append_history(side="buyer", action="proposed", amount=str(amount), message=message)
    offer.save()
    return JsonResponse({"ok": True, "offer": _offer_card(offer)}, status=201)


@require_GET
def offers_mine(request):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    offers = (
        Offer.objects.filter(Q(buyer_account=wallet) | Q(seller_account=wallet))
        .select_related("listing")
        .order_by("-updated_at")
    )
    return JsonResponse({"ok": True, "offers": [_offer_card(o) for o in offers]})


@require_POST
def offer_counter(request, offer_id: int):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    offer, error = _offer_parties(offer_id, wallet, "seller")
    if error is not None:
        return error
    guard = _offer_state_guard(offer, Offer.OPEN_STATUSES)
    if guard is not None:
        return guard
    body = read_json(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    amount = _parse_offer_amount(body.get("amount"))
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "amount must be greater than zero"}, status=400)
    message = str(body.get("message") or "")[:1000]
    offer.amount = str(amount)
    offer.status = "countered"
    offer.append_history(side="seller", action="countered", amount=str(amount), message=message)
    offer.save(update_fields=["amount", "status", "history", "updated_at"])
    return JsonResponse({"ok": True, "offer": _offer_card(offer)})


@require_POST
def offer_accept_counter(request, offer_id: int):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    offer, error = _offer_parties(offer_id, wallet, "buyer")
    if error is not None:
        return error
    guard = _offer_state_guard(offer, ("countered",))
    if guard is not None:
        return guard
    offer.accept(side="buyer")
    return JsonResponse({"ok": True, "offer": _offer_card(offer)})


@require_POST
def offer_accept(request, offer_id: int):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    offer, error = _offer_parties(offer_id, wallet, "seller")
    if error is not None:
        return error
    guard = _offer_state_guard(offer, Offer.OPEN_STATUSES)
    if guard is not None:
        return guard
    offer.accept(side="seller")
    return JsonResponse({"ok": True, "offer": _offer_card(offer)})


@require_POST
def offer_reject(request, offer_id: int):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    offer, error = _offer_parties(offer_id, wallet, "seller")
    if error is not None:
        return error
    guard = _offer_state_guard(offer, Offer.OPEN_STATUSES)
    if guard is not None:
        return guard
    offer.status = "rejected"
    offer.append_history(side="seller", action="rejected")
    offer.save(update_fields=["status", "history", "updated_at"])
    return JsonResponse({"ok": True, "offer": _offer_card(offer)})


@require_POST
def offer_withdraw(request, offer_id: int):
    wallet = current_wallet(request)
    if not wallet:
        return JsonResponse({"ok": False, "error": "connect your wallet first"}, status=401)
    offer, error = _offer_parties(offer_id, wallet, "buyer")
    if error is not None:
        return error
    guard = _offer_state_guard(offer, Offer.OPEN_STATUSES)
    if guard is not None:
        return guard
    offer.status = "withdrawn"
    offer.append_history(side="buyer", action="withdrawn")
    offer.save(update_fields=["status", "history", "updated_at"])
    return JsonResponse({"ok": True, "offer": _offer_card(offer)})
