from __future__ import annotations

import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.escrow import near
from apps.escrow.models import EscrowAgreement, ContractEvent
from apps.wallets.views import current_wallet

from . import carriers
from .models import LogisticsPartner, PartnerApiCredential, Shipment, ShipmentEvent, VerificationRequest


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


def directory(request):
    partners = LogisticsPartner.objects.filter(status="ACTIVE")
    return render(request, "logistics_directory.html", {"partners": partners, "wallet_account": current_wallet(request)})


def partner_register(request):
    wallet = current_wallet(request)
    if not wallet:
        return redirect("/?connect=1")
    if request.method == "POST":
        partner = LogisticsPartner.objects.create(
            company_name=request.POST.get("company_name", "").strip(),
            registration_number=request.POST.get("registration_number", "").strip(),
            email=request.POST.get("email", "").strip(),
            website=request.POST.get("website", "").strip(),
            business_address=request.POST.get("business_address", "").strip(),
            contact_person=request.POST.get("contact_person", "").strip(),
            phone=request.POST.get("phone", "").strip(),
            operating_locations=request.POST.get("operating_locations", "").strip(),
            services=request.POST.get("services", "").strip(),
            tracking_api_endpoint=request.POST.get("tracking_api_endpoint", "").strip(),
            settlement_wallet=request.POST.get("settlement_wallet", wallet).strip() or wallet,
            pricing_note=request.POST.get("pricing_note", "").strip(),
            owner_wallet=wallet,
            status="PENDING",
        )
        messages.success(request, f"Application for {partner.company_name} submitted — an admin will review it.")
        return redirect("partner-portal")
    return render(request, "partner_register.html", {"wallet_account": wallet})


def partner_portal(request):
    wallet = current_wallet(request)
    if not wallet:
        return redirect("/?connect=1")
    partners = LogisticsPartner.objects.filter(owner_wallet=wallet)
    if not partners.exists():
        return render(request, "partner_gate.html", {"wallet_account": wallet})
    shipments = Shipment.objects.filter(provider__in=partners).select_related("provider")
    settlements = [
        {"escrow_id": s.escrow_id, "amount": s.logistics_amount, "state": s.state}
        for s in EscrowAgreement.objects.filter(chain_snapshot__logistics_paid=True)
    ]
    credentials = PartnerApiCredential.objects.filter(partner__in=partners, active=True)
    return render(
        request,
        "partner_portal.html",
        {
            "partners": partners,
            "shipments": shipments,
            "settlements": settlements,
            "credentials": credentials,
            "wallet_account": wallet,
        },
    )


# ---------------------------------------------------------------------------
# JSON API (wallet session)
# ---------------------------------------------------------------------------


@csrf_exempt
@require_POST
@wallet_required_json
def api_create_shipment(request):
    """Register a shipment for a funded escrow (Demo Carrier tracking)."""
    body = json.loads(request.body or "{}")
    escrow_id = (body.get("escrow_id") or "").strip()
    agreement = get_object_or_404(EscrowAgreement, escrow_id=escrow_id)
    chain = near.get_escrow(escrow_id)
    if not chain:
        return JsonResponse({"ok": False, "error": "escrow missing on-chain"}, status=404)
    if chain["state"] not in ("Funded", "ShipmentPending"):
        return JsonResponse({"ok": False, "error": f"escrow is {chain['state']}, expected Funded"}, status=400)
    if request.wallet not in (chain["seller"], chain.get("logistics_provider")):
        return JsonResponse({"ok": False, "error": "only the seller or provider can register a shipment"}, status=403)

    carrier = carriers.get_carrier()
    created = carrier.create_shipment(escrow_id, body.get("origin", "Lagos"), body.get("destination", "Abuja"))
    provider = LogisticsPartner.objects.filter(settlement_wallet=chain.get("logistics_provider") or "").first()
    shipment, was_created = Shipment.objects.get_or_create(
        tracking_number=created["tracking_number"],
        defaults={
            "escrow_id": escrow_id,
            "provider": provider,
            "carrier": "demo",
            "status": "CREATED",
            "simulated": True,
            "registered_by": request.wallet,
        },
    )
    if was_created:
        ShipmentEvent.objects.create(shipment=shipment, status="CREATED", note="Tracking created (simulated)", simulated=True)
        ContractEvent.objects.create(
            escrow_id=escrow_id, event_type="shipment_registered", payload={"tracking_number": shipment.tracking_number}
        )
    return JsonResponse(
        {
            "ok": True,
            "shipment_id": shipment.id,
            "tracking_number": shipment.tracking_number,
            "status": shipment.status,
            "simulated": True,
        }
    )


@require_GET
def api_shipment(request, shipment_id: int):
    shipment = get_object_or_404(Shipment, pk=shipment_id)
    carrier = carriers.get_carrier()
    return JsonResponse({"ok": True, "shipment": carrier.tracking(shipment.tracking_number)})


@csrf_exempt
@require_POST
@wallet_required_json
def api_shipment_event(request, shipment_id: int):
    """Advance the Demo Carrier shipment (simulated scan)."""
    shipment = get_object_or_404(Shipment, pk=shipment_id)
    partner = shipment.provider
    if partner and partner.owner_wallet and partner.owner_wallet != request.wallet:
        return JsonResponse({"ok": False, "error": "not your shipment"}, status=403)
    body = json.loads(request.body or "{}")
    carrier = carriers.get_carrier()
    event = carrier.advance(shipment, to_status=body.get("status"), note=body.get("note", ""))
    return JsonResponse({"ok": True, "status": event.status, "simulated": True})


@csrf_exempt
@require_POST
@wallet_required_json
def api_verification_payload(request):
    """Build the carrier payload the frontend passes to the contract's
    request_delivery_verification call and record the request."""
    body = json.loads(request.body or "{}")
    escrow_id = (body.get("escrow_id") or "").strip()
    shipment = Shipment.objects.filter(escrow_id=escrow_id).order_by("-created_at").first()
    if not shipment:
        return JsonResponse({"ok": False, "error": "no shipment registered for this escrow"}, status=404)
    carrier = carriers.get_carrier()
    payload = carrier.tracking(shipment.tracking_number)
    vr = VerificationRequest.objects.create(
        escrow_id=escrow_id,
        shipment=shipment,
        status="PENDING",
        requested_by=request.wallet,
        input_payload=payload,
        attestation_note="OutLayer TEE (Intel TDX) — genuine attestation; carrier data is SIMULATED Demo Carrier.",
    )
    return JsonResponse({"ok": True, "verification_id": vr.id, "carrier_payload": json.dumps(payload), "simulated": True})


@csrf_exempt
@require_POST
@wallet_required_json
def api_verification_sync(request):
    """Re-read the escrow and update the verification record from chain."""
    body = json.loads(request.body or "{}")
    escrow_id = (body.get("escrow_id") or "").strip()
    vr = VerificationRequest.objects.filter(escrow_id=escrow_id).order_by("-created_at").first()
    if not vr:
        return JsonResponse({"ok": False, "error": "no verification request recorded"}, status=404)
    chain = near.get_escrow(escrow_id)
    last = (chain or {}).get("last_verification")
    if last:
        vr.result = last
        vr.tx_hash = body.get("tx_hash", "")
        vr.status = "VERIFIED" if last.get("verified") else "FAILED"
        vr.completed_at = timezone.now()
        vr.save()
        ContractEvent.objects.create(
            escrow_id=escrow_id,
            event_type="verification_recorded",
            tx_hash=vr.tx_hash,
            payload=last,
        )
    return JsonResponse({"ok": True, "status": vr.status, "result": vr.result, "escrow_state": (chain or {}).get("state")})


# ---------------------------------------------------------------------------
# Partner API (API-key authenticated; used by automated provider systems)
# ---------------------------------------------------------------------------


def _api_partner(request):
    key = request.headers.get("X-Api-Key", "")
    cred = PartnerApiCredential.verify_key(key)
    return cred.partner if cred else None


@require_GET
def api_partner_shipments(request):
    partner = _api_partner(request)
    if not partner:
        return JsonResponse({"ok": False, "error": "invalid API key"}, status=401)
    shipments = Shipment.objects.filter(provider=partner)
    return JsonResponse(
        {
            "ok": True,
            "simulated": True,
            "shipments": [
                {"id": s.id, "escrow_id": s.escrow_id, "tracking_number": s.tracking_number, "status": s.status}
                for s in shipments
            ],
        }
    )


@csrf_exempt
@require_POST
def api_partner_event(request, shipment_id: int):
    partner = _api_partner(request)
    if not partner:
        return JsonResponse({"ok": False, "error": "invalid API key"}, status=401)
    shipment = get_object_or_404(Shipment, pk=shipment_id, provider=partner)
    body = json.loads(request.body or "{}")
    status_value = (body.get("status") or "").upper()
    valid = {choice[0] for choice in Shipment.STATUS_CHOICES}
    if status_value not in valid:
        return JsonResponse({"ok": False, "error": f"status must be one of {sorted(valid)}"}, status=400)
    carrier = carriers.get_carrier()
    event = carrier.advance(shipment, to_status=status_value, note=body.get("note", "Partner-reported event"))
    return JsonResponse({"ok": True, "status": event.status, "simulated": True})
