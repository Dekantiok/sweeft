"""Agent tool registry.

Every tool is one of:
  read    — returns data the agent summarizes for the user
  prepare — returns an ACTION CARD; the user completes the step in the wallet

The co-pilot never holds user keys. The ONE money-moving capability works
through the token's on-chain allowance: the user signs a single `approve`
granting the agent a scoped budget, then the agent may pay within it
(payments are recorded as `PaymentIntent` rows for audit).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.escrow import near
from apps.logistics.models import LogisticsPartner, Shipment, VerificationRequest
from apps.market.models import Listing, Offer, PurchaseRequest

from .agent_wallet import agent_configured


class ToolError(ValueError):
    pass


ACCOUNT_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\.(?:testnet|near)$")


# ---------------------------------------------------------------------------
# Reputation helpers
# ---------------------------------------------------------------------------


def _reputation_of(account_id: str) -> dict:
    try:
        return near.view_call(settings.SWEEFT_CONTRACT, "reputation_of", {"account_id": account_id}) or {}
    except Exception:  # noqa: BLE001
        return {}


def score_label(rep: dict) -> str:
    """Human, factual trust summary (no invented scores)."""
    completed = int(rep.get("completed_as_seller", 0) or 0)
    as_buyer = int(rep.get("completed_as_buyer", 0) or 0)
    disputes = int(rep.get("disputes_opened", 0) or 0)
    volume = near.format_near_amount(rep.get("volume") or 0)
    if completed + as_buyer == 0:
        return "New wallet — no completed trades yet"
    return f"{completed} completed sales, {as_buyer} completed purchases, {volume} NEAR volume, {disputes} disputes"


def rank_score(rep: dict) -> int:
    """Deterministic ranking input for marketplace search results."""
    completed = int(rep.get("completed_as_seller", 0) or 0)
    as_buyer = int(rep.get("completed_as_buyer", 0) or 0)
    disputes = int(rep.get("disputes_opened", 0) or 0)
    refunds = int(rep.get("refunds_claimed", 0) or 0)
    return completed * 10 + as_buyer * 4 - disputes * 5 - refunds * 3


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _token_candidates(token: str) -> set[str]:
    """Small singularization set so plurals match singular listing titles."""
    candidates = {token}
    if len(token) > 3 and token.endswith("s"):
        candidates.add(token[:-1])
    if len(token) > 4 and token.endswith("ies"):
        candidates.add(token[:-3] + "y")
    return candidates


def _listing_reasons(listing: Listing, tokens: list[str], max_price: float | None, kind: str | None, rep: dict) -> list[str]:
    """Deterministic one-liners explaining why a listing matched (no guessing)."""
    reasons: list[str] = []
    haystack = " ".join(
        [listing.title or "", listing.description or "", listing.category or "", listing.location or ""]
    ).lower()
    for token in tokens:
        if any(candidate in haystack for candidate in _token_candidates(token)):
            reasons.append(f"matches “{token}”")
            if len(reasons) >= 2:
                break
    if max_price is not None:
        reasons.append(f"within the {max_price:g} NEAR budget")
    if kind in ("goods", "service"):
        reasons.append(f"type: {kind}")
    completed = int(rep.get("completed_as_seller", 0) or 0)
    if completed:
        reasons.append(f"seller completed {completed} sale(s)")
    elif rank_score(rep) <= 0:
        reasons.append("new seller — no completed sales yet")
    else:
        reasons.append(f"seller trust rank {rank_score(rep)}")
    return reasons


def search_listings(*, query: str = "", max_price: float | None = None, kind: str | None = None, limit: int = 8) -> dict:
    """Find ACTIVE listings and attach each seller's live on-chain trust record.

    Matching is per token (AND across tokens) with light singularization, so
    "solar inverters" matches "Solar inverter batch". The top 5 results each
    carry plain reasons (matched tokens, budget, type, seller record).
    """
    tokens = [token.strip().lower() for token in (query or "").split() if token.strip()]
    qs = Listing.objects.filter(status="active").select_related()
    for token in tokens:
        token_q = Q()
        for candidate in _token_candidates(token):
            token_q |= (
                Q(title__icontains=candidate)
                | Q(description__icontains=candidate)
                | Q(category__icontains=candidate)
                | Q(location__icontains=candidate)
            )
        qs = qs.filter(token_q)
    if kind in ("goods", "service"):
        qs = qs.filter(kind=kind)
    listings = list(qs.distinct()[:60])
    if max_price is not None:
        cap = near.parse_near_amount(str(max_price))
        listings = [l for l in listings if int(l.price_amount) <= cap]
    listings = listings[:limit]

    results = []
    for l in listings:
        rep = _reputation_of(l.seller_account)
        results.append(
            {
                "slug": l.slug,
                "title": l.title,
                "kind": l.kind,
                "category": l.category,
                "price": l.price_display,
                "price_amount": l.price_amount,
                "price_display": l.price_display,
                "location": l.location,
                "seller": l.seller_account,
                "seller_score": score_label(rep),
                "rank": rank_score(rep),
                "reasons": _listing_reasons(l, tokens, max_price, kind, rep),
                "url": reverse("listing-detail", kwargs={"slug": l.slug}),
            }
        )
    results.sort(key=lambda r: r["rank"], reverse=True)
    results = results[:5]
    return {
        "type": "search_results",
        "results": results,
        "count": len(results),
        "query": query,
        "max_price": max_price,
        "kind": kind,
    }


def compare_listings(*, slugs: list[str]) -> dict:
    """Factual side-by-side comparison of 2–4 listings (no invented claims)."""
    cleaned: list[str] = []
    for slug in slugs or []:
        text = str(slug or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if not 2 <= len(cleaned) <= 4:
        raise ToolError("give me 2–4 listing slugs to compare")
    found = {listing.slug: listing for listing in Listing.objects.filter(slug__in=cleaned)}
    missing = [slug for slug in cleaned if slug not in found]
    if missing:
        raise ToolError(f"listing(s) not found: {', '.join(missing)}")
    listings = [found[slug] for slug in cleaned]

    reps = {listing.slug: _reputation_of(listing.seller_account) for listing in listings}
    ranks = {listing.slug: rank_score(reps[listing.slug]) for listing in listings}
    prices = {listing.slug: int(listing.price_amount) for listing in listings}
    best_rank = max(ranks.values())
    lowest_price = min(prices.values())

    rows = []
    for listing in listings:
        rep = reps[listing.slug]
        pros: list[str] = []
        if prices[listing.slug] == lowest_price:
            pros.append("lowest price among these")
        if ranks[listing.slug] == best_rank and best_rank > 0:
            pros.append("strongest seller record among these")
        completed = int(rep.get("completed_as_seller", 0) or 0)
        if completed:
            pros.append(f"{completed} completed sale(s)")
        if listing.location:
            pros.append(f"located in {listing.location}")
        pros.append(f"{listing.get_kind_display().lower()} listing")
        rows.append(
            {
                "slug": listing.slug,
                "title": listing.title,
                "price_display": listing.price_display,
                "seller": listing.seller_account,
                "seller_score": score_label(rep),
                "location": listing.location,
                "pros": pros,
            }
        )
    return {
        "type": "comparison",
        "rows": rows,
        "note": "Facts from the listing records and each seller's on-chain trust record — compare at your own judgement.",
    }


def get_seller_score(*, account_id: str) -> dict:
    rep = _reputation_of(account_id)
    return {"account_id": account_id, "summary": score_label(rep), "rank": rank_score(rep), "raw": rep}


def check_my_balance(*, wallet: str) -> dict:
    units = near.native_balance(wallet)
    return {
        "wallet": wallet,
        "balance": units,
        "balance_display": near.format_near_amount(units),
    }


def _offer_card(offer: Offer, *, with_history: bool = False) -> dict:
    """Card shape shared by every offer tool (chat renders ``kind: offer``)."""
    card = {
        "type": "offer",
        "offer_id": offer.id,
        "listing": offer.listing.title,
        "listing_slug": offer.listing.slug,
        "url": reverse("listing-detail", kwargs={"slug": offer.listing.slug}),
        "amount": offer.amount,
        "amount_display": offer.amount_display,
        "max_amount": offer.max_amount,
        "max_display": offer.max_display,
        "status": offer.status,
        "message": offer.message,
        "buyer": offer.buyer_account,
        "seller": offer.seller_account,
    }
    if with_history:
        card["history"] = offer.history
    return card


def _near_units(value, field: str = "amount") -> int:
    try:
        return near.parse_near_amount(str(value))
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be a NEAR amount like 12.5")


def get_my_trade_status(*, wallet: str) -> dict:
    try:
        escrows = near.escrows_for(wallet)
    except Exception:  # noqa: BLE001
        escrows = []
    pending = [e for e in escrows if e.get("state") in ("PendingAcceptance", "Approved", "Funded", "ShipmentPending", "Shipped")]
    completed = [e for e in escrows if e.get("state") == "Completed"]
    disputes = [e for e in escrows if e.get("state") == "ReviewRequired"]

    rows = []
    for escrow in pending:
        row = {"id": escrow["id"], "state": escrow.get("state", ""), "tracking_number": "", "verification": ""}
        try:
            shipment = Shipment.objects.filter(escrow_id=escrow["id"]).first()
            if shipment is not None:
                row["tracking_number"] = shipment.tracking_number
            verification = VerificationRequest.objects.filter(escrow_id=escrow["id"]).order_by("-created_at").first()
            if verification is not None:
                row["verification"] = verification.status
        except Exception:  # noqa: BLE001 - status card still renders
            pass
        rows.append(row)

    offers = (
        Offer.objects.filter(Q(buyer_account=wallet) | Q(seller_account=wallet), status__in=Offer.OPEN_STATUSES)
        .select_related("listing")
        .order_by("-updated_at")
    )
    offer_rows = [
        {
            "offer_id": offer.id,
            "listing": offer.listing.title,
            "listing_slug": offer.listing.slug,
            "amount_display": offer.amount_display,
            "max_display": offer.max_display,
            "status": offer.status,
            "side": "buyer" if offer.buyer_account == wallet else "seller",
        }
        for offer in offers
    ]
    return {
        "type": "trade_status",
        "escrows": rows,
        "offers": offer_rows,
        # Backwards-compatible summary keys.
        "open_escrows": [e["id"] for e in pending],
        "completed_count": len(completed),
        "disputes": [e["id"] for e in disputes],
    }


def send_purchase_request(*, listing_slug: str, wallet: str, message: str = "") -> dict:
    """Create the off-chain purchase request — the only write-side agent action.

    Seller still issues the on-chain escrow; acceptance and funding are signed
    by the buyer in the wallet UI afterwards.
    """
    listing = Listing.objects.filter(slug=listing_slug, status="active").first()
    if not listing:
        raise ToolError("listing not found or no longer active")
    if wallet == listing.seller_account:
        raise ToolError("you are the seller of this listing — you cannot buy your own item")
    req, created = PurchaseRequest.objects.get_or_create(
        listing=listing,
        buyer_account=wallet,
        status="pending",
        defaults={"message": (message or "")[:1000]},
    )
    return {
        "request_id": req.id,
        "created": created,
        "listing": listing.title,
        "seller": listing.seller_account,
        "url": reverse("listing-detail", kwargs={"slug": listing.slug}),
    }


def create_offer(*, listing_slug: str, amount, max_amount, message: str = "", wallet: str) -> dict:
    """Propose a price to a seller (off-chain record; nothing is signed here)."""
    listing = Listing.objects.filter(slug=listing_slug).first()
    if not listing:
        raise ToolError("listing not found")
    if listing.status != "active":
        raise ToolError("this listing is no longer active")
    if wallet == listing.seller_account:
        raise ToolError("you are the seller of this listing — you cannot make an offer on your own item")
    units = _near_units(amount)
    ceiling = _near_units(max_amount, "max_amount")
    if units <= 0:
        raise ToolError("amount must be greater than zero")
    if ceiling <= 0:
        raise ToolError("max_amount must be greater than zero")
    if units > ceiling:
        raise ToolError("amount cannot exceed the max budget ceiling")
    existing = listing.offers.filter(buyer_account=wallet, status__in=Offer.OPEN_STATUSES).first()
    if existing is not None:
        raise ToolError(f"you already have an open offer (#{existing.id}) on this listing — counter that one instead")
    offer = Offer(
        listing=listing,
        buyer_account=wallet,
        seller_account=listing.seller_account,
        amount=str(units),
        max_amount=str(ceiling),
        message=(message or "")[:1000],
    )
    offer.append_history(side="buyer", action="proposed", amount=str(units), message=offer.message)
    offer.save()
    return _offer_card(offer)


def counter_offer(*, offer_id, amount, wallet: str) -> dict:
    """Buyer counter-back on a seller counteroffer, capped by the buyer ceiling."""
    offer = Offer.objects.filter(pk=offer_id).select_related("listing").first()
    if offer is None:
        raise ToolError("offer not found")
    if offer.buyer_account != wallet:
        raise ToolError("only the buyer can counter this offer")
    if offer.status != "countered":
        raise ToolError(f"offer is {offer.status} — there is no counteroffer to respond to")
    units = _near_units(amount)
    if units <= 0:
        raise ToolError("amount must be greater than zero")
    if units > int(offer.max_amount or 0):
        raise ToolError("limit reached — ask the user to authorize a higher ceiling")
    offer.amount = str(units)
    offer.append_history(side="buyer", action="countered", amount=str(units))
    offer.save(update_fields=["amount", "history", "updated_at"])
    return _offer_card(offer, with_history=True)


def accept_offer(*, offer_id, wallet: str) -> dict:
    """Seller accepts an open offer; the buyer may accept a seller counteroffer."""
    offer = Offer.objects.filter(pk=offer_id).select_related("listing").first()
    if offer is None:
        raise ToolError("offer not found")
    if offer.status not in Offer.OPEN_STATUSES:
        raise ToolError(f"offer is {offer.status} — nothing to accept")
    if offer.seller_account == wallet:
        offer.accept(side="seller")
    elif offer.buyer_account == wallet and offer.status == "countered":
        offer.accept(side="buyer")
    elif offer.buyer_account == wallet:
        raise ToolError("this is your own proposal — the seller has to accept it first")
    else:
        raise ToolError("only the buyer or seller of this offer can accept it")
    return _offer_card(offer, with_history=True)


def reject_offer(*, offer_id, wallet: str) -> dict:
    """Seller rejects an open offer."""
    offer = Offer.objects.filter(pk=offer_id).select_related("listing").first()
    if offer is None:
        raise ToolError("offer not found")
    if offer.seller_account != wallet:
        raise ToolError("only the seller can reject this offer")
    if offer.status not in Offer.OPEN_STATUSES:
        raise ToolError(f"offer is {offer.status} — nothing to reject")
    offer.status = "rejected"
    offer.append_history(side="seller", action="rejected")
    offer.save(update_fields=["status", "history", "updated_at"])
    return _offer_card(offer, with_history=True)


def get_offer_status(*, offer_id, wallet: str) -> dict:
    """Offer card plus the full negotiation history (parties only)."""
    offer = Offer.objects.filter(pk=offer_id).select_related("listing").first()
    if offer is None:
        raise ToolError("offer not found")
    if wallet not in (offer.buyer_account, offer.seller_account):
        raise ToolError("only the buyer or seller of this offer can view it")
    return _offer_card(offer, with_history=True)


def _totals_for_listing(listing: Listing, fee_bps: int | None = None) -> dict:
    price = int(listing.price_amount)
    logistics = price * 100 // 10_000  # 1% default logistics hint; seller sets the final value
    if fee_bps is None:
        try:
            fee_bps = int((near.get_config_cached() or {}).get("platform_fee_bps", 500))
        except Exception:  # noqa: BLE001 - the fee shown is derived from config
            fee_bps = 500
    fee = price * fee_bps // 10_000
    return {
        "seller_amount": str(price),
        "logistics_amount": str(logistics),
        "platform_fee": str(fee),
        "total_amount": str(price + logistics + fee),
    }


def prepare_purchase(*, listing_slug: str, wallet: str) -> dict:
    """Return an ACTION CARD: rank-checked purchase summary, no signatures."""
    listing = Listing.objects.filter(slug=listing_slug, status="active").first()
    if not listing:
        raise ToolError("listing not found or no longer active")
    seller_rep = _reputation_of(listing.seller_account)
    balance = check_my_balance(wallet=wallet)
    totals = _totals_for_listing(listing)
    affordable = int(balance["balance"]) >= int(totals["total_amount"])
    return {
        "type": "purchase_proposal",
        "listing": listing.title,
        "url": reverse("listing-detail", kwargs={"slug": listing.slug}),
        "seller": listing.seller_account,
        "seller_score": score_label(seller_rep),
        "buyer_balance": balance["balance_display"],
        "totals": totals,
        "affordable": affordable,
        "note": "Request the seller to issue the escrow; you then accept and fund it in your wallet. The AI cannot sign for you.",
    }


def prepare_funding(*, escrow_id: str, wallet: str) -> dict:
    """Prepare an accept+fund action card after the seller created the escrow."""
    chain = near.get_escrow(escrow_id)
    if not chain:
        raise ToolError("escrow not found on-chain")
    if chain.get("buyer") != wallet:
        raise ToolError(f"this escrow's buyer is {chain.get('buyer')} — connect that wallet instead")
    return {
        "type": "funding_proposal",
        "escrow_id": escrow_id,
        "state": chain["state"],
        "total_amount": chain["total_amount"],
        "url": reverse("escrow-detail", kwargs={"escrow_id": escrow_id}),
        "note": "Open the escrow and approve in your wallet (accept if still pending, then ft_transfer_call for the exact total).",
    }


def prepare_dispute(*, escrow_id: str, wallet: str, reason: str = "") -> dict:
    """Prefill a dispute case-file card; user submits and signs on-chain."""
    chain = near.get_escrow(escrow_id)
    if not chain:
        raise ToolError("escrow not found on-chain")
    if wallet not in (chain.get("buyer"), chain.get("seller")):
        raise ToolError("only the buyer or seller can open a dispute for this escrow")
    if not reason.strip():
        raise ToolError("describe the problem first (what went wrong?)")
    return {
        "type": "dispute_proposal",
        "escrow_id": escrow_id,
        "reason": reason.strip()[:2000],
        "url": reverse("escrow-detail", kwargs={"escrow_id": escrow_id}),
        "note": "Review the claim, then submit on-chain in your wallet — the AI cannot file it for you.",
    }


# ---------------------------------------------------------------------------
# Co-pilot payments — the ONE money-moving agent capability.
#
# Non-custodial by design: the user grants the agent a NEAR allowance with a
# single wallet-signed `approve`; the token contract enforces the budget for
# every `ft_transfer_from` the agent signs. Revocable at any time by
# approving 0. The agent key itself can never exceed the granted allowance.
# ---------------------------------------------------------------------------


def _onchain_max_payment() -> int:
    """Use the budget contract's cap, falling back to the configured value."""
    settings_cap = int(getattr(settings, "AI_AGENT_MAX_PAYMENT", 100_000_000_000_000_000_000_000))
    try:
        chain_cap = int(near.view_call(settings.AI_BUDGET_CONTRACT, "get_max_payment", {}) or settings_cap)
        return min(settings_cap, chain_cap) if chain_cap > 0 else settings_cap
    except Exception:  # noqa: BLE001 - settings remains the fallback
        return settings_cap


def _onchain_agent() -> str:
    try:
        return near.view_call(settings.AI_BUDGET_CONTRACT, "get_agent", {}) or getattr(settings, "AI_AGENT_ACCOUNT", "")
    except Exception:  # noqa: BLE001 - settings remains the fallback
        return getattr(settings, "AI_AGENT_ACCOUNT", "")


def allowance_of(owner: str) -> int:
    """Unspent native NEAR available in the owner's co-pilot budget."""
    if not owner:
        return 0
    try:
        view = near.view_call(settings.AI_BUDGET_CONTRACT, "budget_of", {"owner": owner}) or {}
        return int(view.get("available") or 0)
    except Exception:  # noqa: BLE001
        return 0


def prepare_payment(*, to: str, amount, memo: str = "", wallet: str) -> dict:
    """Create a payment intent card the co-pilot can execute from its budget."""
    from .models import PaymentIntent

    to = (to or "").strip().lower()
    if not ACCOUNT_ID_RE.match(to) or len(to) > 64:
        raise ToolError("recipient must be a NEAR account id (e.g. seller.steepdata3968.testnet)")
    if to == wallet:
        raise ToolError("that is your own wallet — pick the recipient")

    units = near.parse_near_amount(str(amount))
    if units <= 0:
        raise ToolError("amount must be greater than zero")

    cap = _onchain_max_payment()
    if units > cap:
        raise ToolError(
            f"that is above the co-pilot per-payment limit ({near.format_near_amount(cap)} NEAR). "
            "Split the payment or raise AI_AGENT_MAX_PAYMENT."
        )

    intent = PaymentIntent.objects.create(
        wallet=wallet,
        to_account=to,
        amount=str(units),
        memo=(memo or "").strip()[:200],
        expires_at=timezone.now()
        + timedelta(seconds=int(getattr(settings, "AI_PAYMENT_INTENT_TTL_SECONDS", 900))),
    )

    agent_account = _onchain_agent()
    allowance = allowance_of(wallet)
    grant = int(getattr(settings, "AI_AGENT_GRANT", 250_000_000_000_000_000_000_000))
    return {
        "type": "payment_proposal",
        "intent_id": intent.id,
        "to": to,
        "amount": str(units),
        "amount_display": near.format_near_amount(units),
        "memo": intent.memo,
        "agent": agent_account,
        "budget_contract": settings.AI_BUDGET_CONTRACT,
        "allowance": str(allowance),
        "allowance_display": near.format_near_amount(allowance),
        "can_execute": agent_configured()
        and agent_account == getattr(settings, "AI_AGENT_ACCOUNT", "")
        and allowance >= units,
        "grant_amount": str(grant),
        "grant_display": near.format_near_amount(grant),
        "max_payment_display": near.format_near_amount(cap),
        "note": (
            "The co-pilot pays this from the native NEAR budget you deposit — you set the exact limit; "
            "withdraw the unspent remainder any time to revoke. The budget contract enforces it on-chain."
        ),
    }


# ---------------------------------------------------------------------------
# Registry — tool names are the only strings the LLM may request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str  # read | prepare | write
    description: str
    handler: object


TOOLS: dict[str, ToolSpec] = {
    "search_listings": ToolSpec(
        name="search_listings",
        kind="read",
        description="Search ACTIVE marketplace listings by text/max_price/kind, ranked by seller on-chain trust record (top 5 with plain reasons).",
        handler=search_listings,
    ),
    "compare_listings": ToolSpec(
        name="compare_listings",
        kind="read",
        description="Compare 2–4 listings side by side (price, seller trust, location, factual pros). Requires slugs (list).",
        handler=compare_listings,
    ),
    "get_seller_score": ToolSpec(
        name="get_seller_score",
        kind="read",
        description="Get a wallet's on-chain trust summary (completed sales/purchases, volume, disputes).",
        handler=get_seller_score,
    ),
    "check_my_balance": ToolSpec(
        name="check_my_balance",
        kind="read",
        description="Check the connected wallet's NEAR balance to see whether a purchase is affordable.",
        handler=check_my_balance,
    ),
    "get_my_trade_status": ToolSpec(
        name="get_my_trade_status",
        kind="read",
        description="List the connected wallet's open escrows (state, tracking, verification) and open offers.",
        handler=get_my_trade_status,
    ),
    "send_purchase_request": ToolSpec(
        name="send_purchase_request",
        kind="prepare",
        description="File a purchase request for a listing (off-chain record). Requires listing_slug.",
        handler=send_purchase_request,
    ),
    "prepare_purchase": ToolSpec(
        name="prepare_purchase",
        kind="prepare",
        description="Prepare a purchase proposal card with seller score, total cost and affordability. Requires listing_slug.",
        handler=prepare_purchase,
    ),
    "create_offer": ToolSpec(
        name="create_offer",
        kind="write",
        description=(
            "Make a price offer on a listing (buyer only; off-chain, no funds move). "
            "Requires listing_slug, amount (NEAR) and max_amount (buyer ceiling, NEAR). Optional message."
        ),
        handler=create_offer,
    ),
    "counter_offer": ToolSpec(
        name="counter_offer",
        kind="write",
        description="Buyer counters a seller counteroffer; amount must stay within the buyer's max_amount ceiling. Requires offer_id and amount (NEAR).",
        handler=counter_offer,
    ),
    "accept_offer": ToolSpec(
        name="accept_offer",
        kind="write",
        description="Seller accepts a proposed/countered offer, or the buyer accepts a seller counteroffer. Requires offer_id.",
        handler=accept_offer,
    ),
    "reject_offer": ToolSpec(
        name="reject_offer",
        kind="write",
        description="Seller rejects an open offer. Requires offer_id.",
        handler=reject_offer,
    ),
    "get_offer_status": ToolSpec(
        name="get_offer_status",
        kind="read",
        description="Offer card plus the full negotiation history (buyer or seller only). Requires offer_id.",
        handler=get_offer_status,
    ),
    "prepare_funding": ToolSpec(
        name="prepare_funding",
        kind="prepare",
        description="Prepare an accept+fund action card for an existing escrow owned by the buyer. Requires escrow_id.",
        handler=prepare_funding,
    ),
    "prepare_dispute": ToolSpec(
        name="prepare_dispute",
        kind="prepare",
        description="Prepare a dispute case-file card with a user-supplied reason. Requires escrow_id and reason.",
        handler=prepare_dispute,
    ),
    "prepare_payment": ToolSpec(
        name="prepare_payment",
        kind="prepare",
        description=(
            "Propose paying a NEAR account in NEAR from the budget the user granted the co-pilot "
            "(one wallet approval). Requires to (account id) and amount (in NEAR). Optional memo. "
            "Capped by the platform per-payment limit and by the user's on-chain allowance."
        ),
        handler=prepare_payment,
    ),
}


def execute_tool(name: str, args: dict, wallet: str) -> dict:
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError(f"unknown tool '{name}'")
    if not isinstance(args, dict):
        raise ToolError("tool args must be an object")
    if spec.name == "search_listings":
        kwargs = {}
        if args.get("query") is not None:
            kwargs["query"] = str(args["query"])[:120]
        if args.get("max_price") is not None:
            try:
                kwargs["max_price"] = float(args["max_price"])
            except (TypeError, ValueError):
                raise ToolError("max_price must be a number")
        if args.get("kind"):
            kwargs["kind"] = str(args["kind"])
        return spec.handler(**kwargs)
    if spec.name == "get_seller_score":
        acct = str(args.get("account_id") or "").strip().lower()
        if not acct:
            raise ToolError("account_id is required")
        return spec.handler(account_id=acct)
    if spec.name == "check_my_balance":
        return spec.handler(wallet=wallet)
    if spec.name == "get_my_trade_status":
        return spec.handler(wallet=wallet)
    if spec.name == "compare_listings":
        slugs = args.get("slugs")
        if not isinstance(slugs, list):
            raise ToolError("slugs must be a list of 2–4 listing slugs")
        cleaned = []
        for slug in slugs:
            text = str(slug or "").strip()
            if text:
                cleaned.append(text)
        if not 2 <= len(cleaned) <= 4:
            raise ToolError("give me 2–4 listing slugs to compare")
        return spec.handler(slugs=cleaned)
    if spec.name == "create_offer":
        slug = str(args.get("listing_slug") or args.get("slug") or "").strip()
        if not slug:
            raise ToolError("listing_slug is required")
        if args.get("amount") is None or str(args.get("amount")).strip() == "":
            raise ToolError("amount is required")
        if args.get("max_amount") is None or str(args.get("max_amount")).strip() == "":
            raise ToolError("max_amount is required")
        return spec.handler(
            listing_slug=slug,
            amount=args["amount"],
            max_amount=args["max_amount"],
            message=str(args.get("message") or "")[:1000],
            wallet=wallet,
        )
    if spec.name in ("counter_offer", "accept_offer", "reject_offer", "get_offer_status"):
        try:
            offer_id = int(args.get("offer_id"))
        except (TypeError, ValueError):
            raise ToolError("offer_id must be an integer")
        kwargs = {"offer_id": offer_id, "wallet": wallet}
        if spec.name == "counter_offer":
            if args.get("amount") is None or str(args.get("amount")).strip() == "":
                raise ToolError("amount is required")
            kwargs["amount"] = args["amount"]
        return spec.handler(**kwargs)
    if spec.name in ("send_purchase_request", "prepare_purchase"):
        slug = str(args.get("listing_slug") or args.get("slug") or "").strip()
        if not slug:
            raise ToolError("listing_slug is required")
        kwargs = {"listing_slug": slug, "wallet": wallet}
        if spec.name == "send_purchase_request":
            kwargs["message"] = str(args.get("message") or "")[:1000]
        return spec.handler(**kwargs)
    if spec.name == "prepare_funding":
        eid = str(args.get("escrow_id") or "").strip()
        if not eid:
            raise ToolError("escrow_id is required")
        return spec.handler(escrow_id=eid, wallet=wallet)
    if spec.name == "prepare_dispute":
        eid = str(args.get("escrow_id") or "").strip()
        reason = str(args.get("reason") or "").strip()
        return spec.handler(escrow_id=eid, wallet=wallet, reason=reason)
    if spec.name == "prepare_payment":
        to = str(args.get("to") or args.get("account_id") or "").strip()
        amount = args.get("amount")
        if amount is None or str(amount).strip() == "":
            raise ToolError("amount is required")
        memo = str(args.get("memo") or "")[:200]
        return spec.handler(to=to, amount=amount, memo=memo, wallet=wallet)
    raise ToolError(f"tool '{name}' has no handler")
