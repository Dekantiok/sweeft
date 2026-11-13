from __future__ import annotations

import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.escrow import near
from apps.escrow.models import EscrowAgreement, ContractEvent
from apps.wallets.views import current_wallet

from . import carriers
from .models import LogisticsPartner, PartnerApiCredential, Shipment, ShipmentEvent, VerificationRequest
from .services import sync_verification_from_chain


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


@require_POST
@wallet_required_json
def api_create_shipment(request):
    """Register a shipment for a funded escrow (Demo Carrier tracking).

    `tracking_number` may be supplied when the escrow was created with a
    pre-registered tracking ID (marketplace flow); otherwise the Demo Carrier
    generates one. `scenario` chooses the simulated run outcome
    (`delivered` | `failed` | `lost`) for the standalone simulator.
    """
    body = json.loads(request.body or "{}")
    escrow_id = (body.get("escrow_id") or "").strip()
    chain = near.get_escrow(escrow_id)
    if not chain:
        return JsonResponse({"ok": False, "error": "escrow missing on-chain"}, status=404)
    if chain["state"] not in ("PendingAcceptance", "Approved", "Funded", "ShipmentPending"):
        return JsonResponse({"ok": False, "error": f"escrow is {chain['state']}"}, status=400)
    if request.wallet not in (chain["seller"], chain.get("logistics_provider")):
        return JsonResponse({"ok": False, "error": "only the seller or provider can register a shipment"}, status=403)

    valid_scenarios = {choice[0] for choice in Shipment.SIMULATION_SCENARIOS}
    scenario = (body.get("scenario") or "delivered").strip().lower()
    if scenario not in valid_scenarios:
        return JsonResponse({"ok": False, "error": f"scenario must be one of {sorted(valid_scenarios)}"}, status=400)

    carrier = carriers.get_carrier()
    provided = (body.get("tracking_number") or "").strip()
    created = carrier.create_shipment(escrow_id, body.get("origin", "Lagos"), body.get("destination", "Abuja"))
    tracking = provided or created["tracking_number"]
    provider = LogisticsPartner.objects.filter(settlement_wallet=chain.get("logistics_provider") or "").first()
    shipment, was_created = Shipment.objects.get_or_create(
        tracking_number=tracking,
        defaults={
            "escrow_id": escrow_id,
            "provider": provider,
            "carrier": "demo",
            "status": "CREATED",
            "simulated": True,
            "simulation_scenario": scenario,
            "registered_by": request.wallet,
        },
    )
    if was_created:
        ShipmentEvent.objects.create(shipment=shipment, status="CREATED", note="Tracking created (simulated)", simulated=True)
        ContractEvent.objects.create(
            escrow_id=escrow_id, event_type="shipment_registered", payload={"tracking_number": shipment.tracking_number}
        )
    elif shipment.status == "CREATED" and shipment.simulation_scenario != scenario:
        # Marketplace flow: the row was pre-registered at escrow creation;
        # the scenario is chosen at dispatch. Only meaningful before the run starts.
        shipment.simulation_scenario = scenario
        shipment.save(update_fields=["simulation_scenario", "updated_at"])
    return JsonResponse(
        {
            "ok": True,
            "shipment_id": shipment.id,
            "tracking_number": shipment.tracking_number,
            "status": shipment.status,
            "scenario": shipment.simulation_scenario,
            "simulated": True,
        }
    )


@require_GET
def api_shipment(request, shipment_id: int):
    shipment = get_object_or_404(Shipment, pk=shipment_id)
    carrier = carriers.get_carrier()
    return JsonResponse({"ok": True, "shipment": carrier.tracking(shipment.tracking_number)})


@require_GET
def api_shipment_for_escrow(request, escrow_id: str):
    """Latest shipment for an escrow — powers live tracking on the escrow page.

    Includes the escrow's on-chain state (cached read) so the page can detect
    when verification/settlement moves the escrow forward and refresh itself.
    """
    shipment = Shipment.objects.filter(escrow_id=escrow_id).order_by("-created_at").first()
    if not shipment:
        return JsonResponse({"ok": True, "shipment": None})
    carrier = carriers.get_carrier()
    payload = carrier.tracking(shipment.tracking_number)
    payload["id"] = shipment.id
    payload["simulation_scenario"] = shipment.simulation_scenario
    try:
        chain = near.get_escrow_cached(escrow_id)
        payload["escrow_state"] = (chain or {}).get("state")
    except Exception:  # noqa: BLE001 — tracking must render even if RPC hiccups
        payload["escrow_state"] = None
    return JsonResponse({"ok": True, "shipment": payload})


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
    try:
        event = carrier.advance(shipment, to_status=body.get("status"), note=body.get("note", ""))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "status": event.status, "simulated": True})


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
    # The verifier and the contract check the carrier's shipment identity
    # against the on-chain shipment id, so include it in the payload.
    try:
        chain = near.get_escrow(escrow_id) or {}
        payload["shipment_id"] = chain.get("shipment_id")
    except Exception:  # noqa: BLE001
        payload["shipment_id"] = None
    # Carrier-authenticity: signed by the Demo Carrier service key whose
    # public counterpart is registered on-chain for the TEE to verify.
    payload = carriers.sign_carrier_payload(payload)
    vr = VerificationRequest.objects.create(
        escrow_id=escrow_id,
        shipment=shipment,
        status="PENDING",
        requested_by=request.wallet,
        input_payload=payload,
        attestation_note="OutLayer TEE (Intel TDX) — genuine attestation; carrier data is SIMULATED Demo Carrier.",
    )
    return JsonResponse({"ok": True, "verification_id": vr.id, "carrier_payload": json.dumps(payload), "simulated": True})


@require_POST
@wallet_required_json
def api_verification_sync(request):
    """Re-read the escrow and update the verification record from chain."""
    body = json.loads(request.body or "{}")
    escrow_id = (body.get("escrow_id") or "").strip()
    if not VerificationRequest.objects.filter(escrow_id=escrow_id).exists():
        return JsonResponse({"ok": False, "error": "no verification request recorded"}, status=404)
    chain = near.get_escrow(escrow_id)
    vr = sync_verification_from_chain(escrow_id, tx_hash=body.get("tx_hash", ""), chain=chain)
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
    """Shipments assigned to this partner.

    `?include_escrow_state=1` adds the on-chain escrow state (cached RPC read)
    so an automated provider system can gate its run on actual dispatch —
    the standalone Demo Carrier simulator uses this.
    """
    partner = _api_partner(request)
    if not partner:
        return JsonResponse({"ok": False, "error": "invalid API key"}, status=401)
    include_chain = request.GET.get("include_escrow_state") == "1"
    shipments = Shipment.objects.filter(provider=partner).prefetch_related("events")
    rows = []
    for s in shipments:
        row = {
            "id": s.id,
            "escrow_id": s.escrow_id,
            "tracking_number": s.tracking_number,
            "status": s.status,
            "simulation_scenario": s.simulation_scenario,
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
            "events": [
                {"status": e.status, "note": e.note, "timestamp": e.created_at.isoformat()}
                for e in s.events.all()
            ],
        }
        if include_chain:
            try:
                chain = near.get_escrow_cached(s.escrow_id)
                row["escrow_state"] = (chain or {}).get("state")
            except Exception:  # RPC trouble must not break carrier polling
                row["escrow_state"] = None
        rows.append(row)
    return JsonResponse({"ok": True, "simulated": True, "shipments": rows})


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
    try:
        event = carrier.advance(shipment, to_status=status_value, note=body.get("note", "Partner-reported event"))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "status": event.status, "simulated": True})
