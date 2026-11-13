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
from decimal import Decimal

from django.conf import settings
from django.db.models import Q
from django.urls import reverse

from apps.escrow import near
from apps.logistics.models import LogisticsPartner
from apps.market.models import Listing, PurchaseRequest

from .agent_wallet import agent_configured


class ToolError(ValueError):
    pass


ACCOUNT_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\.(?:testnet|near)$")


# ---------------------------------------------------------------------------
# Reputation helpers
# ---------------------------------------------------------------------------


def _reputation_of(account_id: str) -> dict:
    try:
        return near.view_call(settings.VERIPAY_CONTRACT, "reputation_of", {"account_id": account_id}) or {}
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


def search_listings(*, query: str = "", max_price: float | None = None, kind: str | None = None, limit: int = 8) -> dict:
    """Find ACTIVE listings and attach each seller's live on-chain trust record.

    Matching is per token (AND across tokens) with light singularization, so
    "solar inverters" matches "Solar inverter batch".
    """
    qs = Listing.objects.filter(status="active").select_related()
    for token in (query or "").split():
        token = token.strip().lower()
        if not token:
            continue
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
                "seller": l.seller_account,
                "seller_score": score_label(rep),
                "rank": rank_score(rep),
                "url": reverse("listing-detail", kwargs={"slug": l.slug}),
            }
        )
    results.sort(key=lambda r: r["rank"], reverse=True)
    return {"results": results, "count": len(results), "query": query, "max_price": max_price, "kind": kind}


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


def get_my_trade_status(*, wallet: str) -> dict:
    try:
        escrows = near.escrows_for(wallet)
    except Exception:  # noqa: BLE001
        escrows = []
    pending = [e for e in escrows if e.get("state") in ("PendingAcceptance", "Approved", "Funded", "ShipmentPending", "Shipped")]
    completed = [e for e in escrows if e.get("state") == "Completed"]
    disputes = [e for e in escrows if e.get("state") == "ReviewRequired"]
    return {
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


def _totals_for_listing(listing: Listing, fee_bps: int = 500) -> dict:
    price = int(listing.price_amount)
    logistics = price * 100 // 10_000  # 1% default logistics hint; seller sets the final value
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

    cap = int(getattr(settings, "AI_AGENT_MAX_PAYMENT", 100_000_000_000_000_000_000_000))
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
    )

    agent_account = getattr(settings, "AI_AGENT_ACCOUNT", "")
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
        "can_execute": agent_configured() and allowance >= units,
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
    kind: str  # read | prepare
    description: str
    handler: object


TOOLS: dict[str, ToolSpec] = {
    "search_listings": ToolSpec(
        name="search_listings",
        kind="read",
        description="Search ACTIVE marketplace listings by text/max_price/kind, ranked by seller on-chain trust record.",
        handler=search_listings,
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
        description="List the connected wallet's open escrows, completed trades and disputes.",
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
