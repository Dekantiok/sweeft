from __future__ import annotations

import hashlib
import json
import time
import secrets

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.wallets.views import current_wallet

from . import near
from .models import ContractEvent, EscrowAgreement, EscrowInvitation, SettlementRecord


def _wait_for_chain_escrow(escrow_id: str, expected_state: str | None = None, attempts: int = 8, delay: float = 1.5):
    """Poll the chain after a wallet tx so finality lag doesn't orphan the UI."""
    last = None
    for attempt in range(attempts):
        try:
            chain = near.get_escrow(escrow_id)
        except Exception:  # noqa: BLE001 - keep retrying until the deadline
            chain = None
        last = chain
        if chain and (expected_state is None or chain.get("state") == expected_state):
            return chain
        if attempt < attempts - 1:
            time.sleep(delay)
    return last


def config_context(request):
    return {
        "SWEEFT_CONTRACT": settings.SWEEFT_CONTRACT,
        "OUTLAYER_CONTRACT": settings.OUTLAYER_CONTRACT,
        "NEAR_NETWORK": settings.NEAR_NETWORK,
        "EXPLORER_URL": settings.EXPLORER_URL,
        "PLATFORM_OWNER": settings.PLATFORM_OWNER,
    }


def wallet_required_json(view):
    def wrapped(request, *args, **kwargs):
        wallet = current_wallet(request)
        if not wallet:
            return JsonResponse({"ok": False, "error": "wallet not authenticated"}, status=401)
        request.wallet = wallet
        return view(request, *args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def landing(request):
    return render(request, "landing.html")


def dashboard(request):
    wallet = current_wallet(request)
    if not wallet:
        return render(request, "dashboard.html", {"wallet_account": None})

    # Merge the Django projection with live chain data: an escrow exists for
    # this wallet if the contract says so, metadata row or not.
    items: dict[str, dict] = {}
    agreements = EscrowAgreement.objects.filter(seller_account=wallet) | EscrowAgreement.objects.filter(
        buyer_account=wallet
    )
    for a in agreements:
        items[a.escrow_id] = {
            "escrow_id": a.escrow_id,
            "title": a.product_name or "(untitled)",
            "state": a.state,
            "seller": a.seller_account,
            "buyer": a.buyer_account,
            "source": "app",
        }
    chain_error = None
    try:
        for e in near.escrows_for(wallet):
            row = items.get(e["id"])
            if row is None:
                items[e["id"]] = {
                    "escrow_id": e["id"],
                    "title": e.get("product_name") or "(on-chain escrow)",
                    "state": e["state"],
                    "seller": e["seller"],
                    "buyer": e.get("buyer") or "",
                    "source": "chain",
                }
            else:
                row["state"] = e["state"]  # chain state wins
                row["buyer"] = e.get("buyer") or row["buyer"]
    except Exception as exc:  # noqa: BLE001 - page still renders from the projection
        chain_error = str(exc)

    all_items = sorted(items.values(), key=lambda r: r["escrow_id"], reverse=True)
    buying = [r for r in all_items if r["buyer"] == wallet]
    selling = [r for r in all_items if r["seller"] == wallet]

    # Only preassigned invitations appear here; unassigned ones are locators
    # (the link is required), so they are not listed to arbitrary wallets.
    my_invitations = [
        inv
        for inv in EscrowInvitation.objects.filter(status="pending", agreement__buyer_account=wallet)
        if inv.agreement.seller_account != wallet
    ]
    return render(
        request,
        "dashboard.html",
        {
            "buying": buying,
            "selling": selling,
            "invitations": my_invitations,
            "wallet_account": wallet,
            "chain_error": chain_error,
        },
    )


def escrow_new(request):
    wallet = current_wallet(request)
    if not wallet:
        return redirect("/?connect=1")
    # 72h default inspection window: silent buyers keep a real review period
    # before the permissionless timeout release can pay the seller.
    from datetime import datetime, timedelta

    prefill = {
        "counterparty": "",
        "amount": "",
        "logistics": "0.01",
        "deadline_days": "7",
        "deadline_date": (datetime.now().date() + timedelta(days=7)).isoformat(),
        "inspection_days": "3",
        "terms": "",
        "product": "",
        "listing_slug": "",
        "listing_title": "",
        "provider": "",
    }
    listing_id = request.GET.get("listing")
    if listing_id:
        from apps.market.models import Listing  # local import avoids app-loading cycle

        listing = Listing.objects.filter(pk=listing_id, seller_account=wallet).first()
        if listing:
            prefill = {
                "counterparty": (request.GET.get("buyer") or "").strip(),
                "amount": listing.price_display.replace(",", ""),
                "logistics": "0.01",
                "deadline_days": str(listing.dispatch_days),
                "deadline_date": (datetime.now().date() + timedelta(days=listing.dispatch_days)).isoformat(),
                "inspection_days": str(max(1, listing.inspection_days)),
                "terms": f"{listing.title} — listed on the SWEEFT marketplace",
                "product": listing.title,
                "listing_slug": listing.slug,
                "listing_title": listing.title,
                "provider": listing.logistics_provider or "",
            }
    providers = []
    try:
        from apps.logistics.models import LogisticsPartner

        providers = list(LogisticsPartner.objects.filter(status="ACTIVE"))
    except Exception:  # noqa: BLE001
        providers = []
    return render(request, "escrow_new.html", {"wallet_account": wallet, "prefill": prefill, "providers": providers})


def escrow_detail(request, escrow_id: str):
    agreement = EscrowAgreement.objects.filter(escrow_id=escrow_id).first()
    chain = None
    chain_error = None
    try:
        chain = near.get_escrow_cached(escrow_id)
    except Exception as e:  # noqa: BLE001 - surfaced in the UI
        chain_error = str(e)
    if agreement is None and chain:
        # On-chain escrow without a metadata row (e.g. created via CLI):
        # synthesize a read-only record so the page renders.
        agreement = EscrowAgreement(
            escrow_id=escrow_id,
            seller_account=chain["seller"],
            buyer_account=chain.get("buyer") or "",
            product_name=chain.get("product_name") or "(unknown — created outside the web app)",
            seller_amount=chain["seller_amount"],
            logistics_amount=chain["logistics_amount"],
            platform_fee=chain["platform_fee"],
            total_amount=chain["total_amount"],
            token_contract=chain["token"],
            state=chain["state"],
            dispatch_deadline_ms=chain["dispatch_deadline_ms"],
            terms_hash=chain.get("terms_hash", ""),
        )
    if agreement is None and not chain:
        return render(request, "escrow_missing.html", {"escrow_id": escrow_id, "wallet_account": current_wallet(request)}, status=404)
    invitation_link = None
    if agreement and agreement.pk:
        inv = agreement.invitations.filter(status="pending").first()
        if (
            inv
            and chain
            and current_wallet(request) == chain.get("seller")
            and not chain.get("buyer")
            and chain.get("state") == "PendingAcceptance"
        ):
            invitation_link = request.build_absolute_uri(f"/invitations/{inv.token}/")
    owner = None
    try:
        owner = (near.get_config_cached() or {}).get("owner")
    except Exception:  # noqa: BLE001 - page renders without config
        pass
    provider_options = []
    try:
        from apps.logistics.models import LogisticsPartner

        provider_options = [
            {"wallet": p.settlement_wallet, "name": p.company_name}
            for p in LogisticsPartner.objects.filter(status="ACTIVE")
        ]
    except Exception:  # noqa: BLE001
        provider_options = []
    from .models import Dispute

    dispute = Dispute.objects.filter(escrow_id=escrow_id).order_by("-created_at").first()

    # Inspection window: once delivery is TEE-verified, the seller allocation
    # auto-releases after `inspection_period_ms` unless a problem is reported
    # first (permissionless `release_after_inspection`, driven by the relayer).
    auto_release_ms = None
    if chain and chain.get("state") == "AwaitingBuyerConfirmation" and not chain.get("seller_paid"):
        try:
            delivered_ms = near.view_call(settings.SWEEFT_CONTRACT, "delivery_time_ms", {"escrow_id": escrow_id})
        except Exception:  # noqa: BLE001 — pre-upgrade escrows have no clock
            delivered_ms = None
        if delivered_ms:
            auto_release_ms = int(delivered_ms) + int(chain.get("inspection_period_ms") or 0)
    # A stalled review cannot lock funds forever: the contract exposes the
    # dispute start so the buyer can see and claim the seven-day fallback.
    dispute_refund_ms = None
    if chain and chain.get("state") == "ReviewRequired" and not chain.get("seller_paid"):
        try:
            dispute_ms = near.view_call(settings.SWEEFT_CONTRACT, "dispute_time_ms", {"escrow_id": escrow_id})
        except Exception:  # noqa: BLE001 — contracts deployed before the dispute clock
            dispute_ms = None
        if dispute_ms is not None:
            dispute_refund_ms = int(dispute_ms) + 7 * 24 * 60 * 60 * 1000
    relayer_enabled = bool(settings.RELAYER_ENABLED and settings.RELAYER_ACCOUNT and settings.RELAYER_PRIVATE_KEY)

    # Review board (governed dispute resolution). Shown while a dispute is
    # open — proposals, votes and the escalation form live here.
    gov_ctx = None
    if chain and chain.get("state") == "ReviewRequired":
        try:
            from . import gov as gov_mod

            policy = gov_mod.fetch_policy()
            council = gov_mod.council_info(policy)
            wallet = current_wallet(request)
            gov_ctx = {
                "dao": gov_mod.dao_account(),
                "council": council,
                "proposals": gov_mod.proposals_for_escrow(escrow_id),
                "proposal_bond_display": near.format_near_amount(gov_mod.proposal_bond_yocto(policy)),
                "proposal_bond_yocto": str(gov_mod.proposal_bond_yocto(policy)),
                "has_active_proposal": any(p.get("status") == "InProgress" for p in gov_mod.proposals_for_escrow(escrow_id)),
                "is_council": bool(wallet and wallet in (council["members"] or [])),
                "is_party": bool(wallet and wallet in (chain.get("seller"), chain.get("buyer"))),
            }
        except Exception:  # noqa: BLE001
            gov_ctx = None
    return render(
        request,
        "escrow_detail.html",
        {
            "escrow_id": escrow_id,
            "agreement": agreement,
            "chain": chain,
            "chain_json": json.dumps(chain or {}),
            "chain_error": chain_error,
            "wallet_account": current_wallet(request),
            "owner": owner,
            "provider_options": provider_options,
            "invitation_link": invitation_link,
            "dispute": dispute,
            "gov": gov_ctx,
            "auto_release_ms": auto_release_ms,
            "dispute_refund_ms": dispute_refund_ms,
            "relayer_enabled": relayer_enabled,
            "events": ContractEvent.objects.filter(escrow_id=escrow_id)[:20],
            "settlements": SettlementRecord.objects.filter(escrow_id=escrow_id)[:20],
        },
    )


def invitation_view(request, token: str):
    invitation = get_object_or_404(EscrowInvitation, token=token)
    agreement = invitation.agreement
    chain = None
    chain_error = None
    try:
        chain = near.get_escrow(agreement.escrow_id)
    except Exception as e:  # noqa: BLE001
        chain_error = str(e)
    return render(
        request,
        "invitation.html",
        {
            "invitation": invitation,
            "agreement": agreement,
            "chain": chain,
            "chain_error": chain_error,
            "wallet_account": current_wallet(request),
        },
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@require_GET
def api_config(request):
    return JsonResponse(
        {
            "network": settings.NEAR_NETWORK,
            "rpc_url": settings.NEAR_RPC_URL,
            "sweeft_contract": settings.SWEEFT_CONTRACT,
            "outlayer_contract": settings.OUTLAYER_CONTRACT,
            "outlayer_project": settings.OUTLAYER_PROJECT,
            "explorer": settings.EXPLORER_URL,
        }
    )


@require_POST
@wallet_required_json
def api_register_agreement(request):
    """Register off-chain metadata after the seller created the escrow on-chain.

    The chain row is re-read and validated: seller must match the session
    wallet; amounts must match; the invitation token (Case B) must hash to the
    on-chain invite_hash.
    """
    body = json.loads(request.body or "{}")
    escrow_id = (body.get("escrow_id") or "").strip()
    if not escrow_id:
        return JsonResponse({"ok": False, "error": "escrow_id required"}, status=400)

    chain = _wait_for_chain_escrow(escrow_id)
    if not chain:
        return JsonResponse({"ok": False, "error": "escrow not found on-chain"}, status=404)
    if chain["seller"] != request.wallet:
        return JsonResponse({"ok": False, "error": "on-chain seller does not match session wallet"}, status=403)

    invitation_token = (body.get("invitation_token") or "").strip()
    if chain.get("invite_hash"):
        if not invitation_token:
            return JsonResponse({"ok": False, "error": "invitation token required for this escrow"}, status=400)
        if hashlib.sha256(invitation_token.encode()).hexdigest() != chain["invite_hash"]:
            return JsonResponse({"ok": False, "error": "invitation token does not match on-chain hash"}, status=400)

    agreement, _ = EscrowAgreement.objects.update_or_create(
        escrow_id=escrow_id,
        defaults={
            "seller_account": chain["seller"],
            "buyer_account": chain.get("buyer") or "",
            "product_name": body.get("product_name") or chain.get("product_name", ""),
            "product_description": body.get("product_description", ""),
            "product_desc_hash": chain.get("product_desc_hash", ""),
            "seller_amount": chain["seller_amount"],
            "logistics_amount": chain["logistics_amount"],
            "platform_fee": chain["platform_fee"],
            "total_amount": chain["total_amount"],
            "token_contract": chain["token"],
            "state": chain["state"],
            "dispatch_deadline_ms": chain["dispatch_deadline_ms"],
            "inspection_period_ms": chain.get("inspection_period_ms", 0),
            "terms_hash": chain.get("terms_hash", ""),
            "create_tx": body.get("tx_hash", ""),
            "last_synced": timezone.now(),
            "chain_snapshot": chain,
        },
    )
    if invitation_token:
        EscrowInvitation.objects.get_or_create(token=invitation_token, defaults={"agreement": agreement})
    ContractEvent.objects.create(
        escrow_id=escrow_id,
        event_type="escrow_created",
        tx_hash=body.get("tx_hash", ""),
        payload={"seller": chain["seller"], "total": chain["total_amount"]},
    )

    # Marketplace loop: if this escrow came from a listing, mark the listing
    # sold and close any pending purchase request from the named buyer.
    listing_slug = (body.get("listing_slug") or "").strip()
    if listing_slug:
        from apps.market.models import Listing, PurchaseRequest

        listing = Listing.objects.filter(slug=listing_slug, seller_account=chain["seller"]).first()
        if listing and chain.get("state") in ("Approved", "Funded"):
            listing.status = "sold"
            listing.sold_escrow_id = escrow_id
            listing.save(update_fields=["status", "sold_escrow_id", "updated_at"])
        elif listing:
            # A request still in escrow creation should not close the listing
            # until the buyer has accepted and the funds can be committed.
            listing.status = "active"
            listing.save(update_fields=["status", "updated_at"])
            if chain.get("buyer"):
                PurchaseRequest.objects.filter(
                    listing=listing, buyer_account=chain["buyer"], status="pending"
                ).update(status="escrowed", escrow_id=escrow_id)
    return JsonResponse({"ok": True, "escrow_id": escrow_id})


@require_GET
@wallet_required_json
def api_my_agreements(request):
    wallet = request.wallet
    agreements = EscrowAgreement.objects.filter(seller_account=wallet) | EscrowAgreement.objects.filter(
        buyer_account=wallet
    )
    return JsonResponse({"ok": True, "agreements": [serialize_agreement(a) for a in agreements]})


@require_GET
def api_agreement(request, escrow_id: str):
    agreement = EscrowAgreement.objects.filter(escrow_id=escrow_id).first()
    try:
        chain = near.get_escrow(escrow_id)
    except Exception as e:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    if not chain and not agreement:
        return JsonResponse({"ok": False, "error": "unknown escrow"}, status=404)
    return JsonResponse(
        {
            "ok": True,
            "chain": chain,
            "metadata": serialize_agreement(agreement) if agreement else None,
        }
    )


@require_POST
@wallet_required_json
def api_sync_agreement(request, escrow_id: str):
    """Re-read chain state into the projection; record settlement rows."""
    from apps.wallets import ratelimit

    if not ratelimit.allow(f"sync:{ratelimit.client_ip(request)}", settings.RATE_LIMIT_SYNC_PER_MIN):
        return JsonResponse({"ok": False, "error": "too many sync requests — slow down"}, status=429)
    agreement = EscrowAgreement.objects.filter(escrow_id=escrow_id).first()
    try:
        chain = near.get_escrow(escrow_id)
    except Exception as e:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(e)}, status=502)
    if not chain:
        return JsonResponse({"ok": False, "error": "escrow missing on-chain"}, status=404)

    if agreement is None:
        agreement = EscrowAgreement(
            escrow_id=escrow_id,
            seller_account=chain["seller"],
            buyer_account=chain.get("buyer") or "",
            product_name=chain.get("product_name") or "(created outside the web app)",
            seller_amount=chain["seller_amount"],
            logistics_amount=chain["logistics_amount"],
            platform_fee=chain["platform_fee"],
            total_amount=chain["total_amount"],
            token_contract=chain["token"],
            dispatch_deadline_ms=chain["dispatch_deadline_ms"],
            inspection_period_ms=chain.get("inspection_period_ms", 0),
            terms_hash=chain.get("terms_hash", ""),
        )
    agreement.state = chain["state"]
    agreement.buyer_account = chain.get("buyer") or ""
    agreement.last_synced = timezone.now()
    agreement.chain_snapshot = chain
    agreement.save()

    if chain.get("state") in ("Approved", "Funded", "Completed"):
        from apps.market.models import Listing

        from apps.market.models import PurchaseRequest

        request = PurchaseRequest.objects.filter(escrow_id=escrow_id, status__in=("pending", "escrowed")).first()
        listing = request.listing if request else Listing.objects.filter(sold_escrow_id=escrow_id, seller_account=chain["seller"]).first()
        if listing:
            listing.status = "sold"
            listing.sold_escrow_id = escrow_id
            listing.save(update_fields=["status", "sold_escrow_id", "updated_at"])

    _record_settlement(escrow_id, "logistics", chain.get("logistics_provider"), chain.get("logistics_amount"), chain.get("logistics_paid"))
    _record_settlement(escrow_id, "seller", chain.get("seller"), chain.get("seller_amount"), chain.get("seller_paid"))
    if chain.get("platform_paid"):
        try:
            treasury = (near.view_call(settings.SWEEFT_CONTRACT, "get_gov_info", {}) or {}).get("treasury")
        except Exception:  # noqa: BLE001 - projection must not break on config read
            treasury = None
        _record_settlement(escrow_id, "platform", treasury, chain.get("platform_fee"), True)
    return JsonResponse({"ok": True, "state": chain["state"], "chain": chain})


def _record_settlement(escrow_id: str, party: str, account_id, amount, paid: bool):
    if not paid or not account_id or not amount:
        return
    SettlementRecord.objects.get_or_create(
        escrow_id=escrow_id,
        party=party,
        defaults={"account_id": account_id, "amount": str(amount), "status": "paid"},
    )


@require_GET
def api_invitation(request, token: str):
    invitation = get_object_or_404(EscrowInvitation, token=token)
    agreement = invitation.agreement
    chain = None
    try:
        chain = near.get_escrow(agreement.escrow_id)
    except Exception:  # noqa: BLE001
        pass
    return JsonResponse(
        {
            "ok": True,
            "token": token,
            "status": invitation.status,
            "agreement": serialize_agreement(agreement),
            "chain": chain,
        }
    )


@require_POST
@wallet_required_json
def api_invitation_accept(request, token: str):
    """Mark the invitation accepted after the buyer's on-chain accept_escrow tx."""
    invitation = get_object_or_404(EscrowInvitation, token=token)
    chain = _wait_for_chain_escrow(invitation.agreement.escrow_id)
    if not chain:
        return JsonResponse({"ok": False, "error": "escrow missing on-chain"}, status=404)
    if chain.get("buyer") != request.wallet:
        return JsonResponse(
            {"ok": False, "error": "on-chain buyer does not match session wallet — accept on-chain first"},
            status=400,
        )
    invitation.status = "accepted"
    invitation.accepted_at = timezone.now()
    invitation.accepted_by = request.wallet
    invitation.save()
    agreement = invitation.agreement
    agreement.buyer_account = request.wallet
    agreement.state = chain["state"]
    agreement.chain_snapshot = chain
    agreement.last_synced = timezone.now()
    agreement.save()
    ContractEvent.objects.create(
        escrow_id=agreement.escrow_id, event_type="escrow_accepted", tx_hash=request.POST.get("tx_hash", "")
    )
    return JsonResponse({"ok": True})


def serialize_agreement(a: EscrowAgreement) -> dict:
    return {
        "escrow_id": a.escrow_id,
        "seller_account": a.seller_account,
        "buyer_account": a.buyer_account,
        "product_name": a.product_name,
        "product_description": a.product_description,
        "seller_amount": a.seller_amount,
        "logistics_amount": a.logistics_amount,
        "platform_fee": a.platform_fee,
        "total_amount": a.total_amount,
        "token_contract": a.token_contract,
        "state": a.state,
        "dispatch_deadline_ms": a.dispatch_deadline_ms,
        "created_at": a.created_at.isoformat(),
    }


def new_invitation_token() -> str:
    return secrets.token_urlsafe(32)
