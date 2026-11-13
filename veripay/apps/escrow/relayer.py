"""Verification relayer — platform-side automation for the happy path.

The escrow contract gates every step and fixes every destination and amount at
creation time, so this relayer can only trigger pre-agreed, condition-gated
movements — it can never redirect funds:

  1. carrier reports DELIVERED   → request TEE verification (permissionless);
  2. the TEE result lands on-chain → sync the outcome into the projection;
  3. pickup is TEE-verified       → pay the logistics allocation (permissionless);
  4. inspection window elapsed    → release seller + platform allocations
     (permissionless), unless a problem was reported.

Every action re-reads the chain first, so the loop is idempotent and safe to
run continuously (``manage.py run_relayer``) or once (``--once``).

Trust disclosure (testnet MVP): ``RELAYER_PRIVATE_KEY`` lives on the demo
server because this is a testnet build; on mainnet it would run inside a
TEE/KMS. Compromise of the key cannot move funds anywhere not already
committed to by the escrow state machine.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from django.conf import settings

from apps.logistics import carriers
from apps.logistics.models import Shipment, VerificationRequest
from apps.logistics.services import sync_verification_from_chain

from . import near
from .server_tx import call_function_as

RELAYER_GAS = 150_000_000_000_000  # 150 Tgas: pays for the OutLayer call + callback


def relayer_configured() -> bool:
    return bool(
        getattr(settings, "RELAYER_ENABLED", False)
        and getattr(settings, "RELAYER_ACCOUNT", "")
        and getattr(settings, "RELAYER_PRIVATE_KEY", "")
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _send(method: str, args: dict, deposit: int = 0) -> dict:
    """Sign one escrow-contract call. Non-payable methods require exactly 0
    deposit; only `request_delivery_verification` is given a compute deposit."""
    return call_function_as(
        signer=settings.RELAYER_ACCOUNT,
        private_key=settings.RELAYER_PRIVATE_KEY,
        method_name=method,
        args=args,
        receiver_id=settings.VERIPAY_CONTRACT,
        gas=RELAYER_GAS,
        deposit=deposit,
    )


@dataclass
class RelayerReport:
    requested: list[str] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    logistics_paid: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out: list[str] = []
        for label, ids in (
            ("requested verification", self.requested),
            ("synced result", self.synced),
            ("paid logistics", self.logistics_paid),
            ("released after inspection", self.released),
        ):
            if ids:
                out.append(f"{label}: {', '.join(ids)}")
        out.extend(f"error — {message}" for message in self.errors)
        return out or ["nothing to do"]


def relayer_tick(limit: int = 20) -> RelayerReport:
    """One pass over shipments whose carrier reports DELIVERED."""
    report = RelayerReport()
    if not relayer_configured():
        report.errors.append("relayer is not configured")
        return report

    shipments = (
        Shipment.objects.filter(status="DELIVERED")
        .select_related("provider")
        .order_by("-updated_at")[:limit]
    )
    seen: set[str] = set()
    for shipment in shipments:
        if shipment.escrow_id in seen:
            continue  # several shipments can reference one escrow — act once
        seen.add(shipment.escrow_id)
        try:
            _advance_one(shipment, report)
        except Exception as e:  # noqa: BLE001 — one bad escrow must not stop the loop
            report.errors.append(f"{shipment.escrow_id}: {e}")
    return report


def _advance_one(shipment: Shipment, report: RelayerReport) -> None:
    escrow_id = shipment.escrow_id
    chain = near.get_escrow(escrow_id)
    if not chain:
        return
    state = chain.get("state")

    # 1) Ask for TEE verification once the carrier's final scan is DELIVERED.
    if (
        state == "Shipped"
        and not chain.get("verification_pending")
        and not chain.get("delivered_verified")
    ):
        payload = carriers.get_carrier().tracking(shipment.tracking_number)
        # Contract-side identity checks need the on-chain shipment id.
        payload["shipment_id"] = chain.get("shipment_id")
        if payload.get("status") != "DELIVERED":
            return  # wait for the carrier's final scan
        if not VerificationRequest.objects.filter(escrow_id=escrow_id, status="PENDING").exists():
            VerificationRequest.objects.create(
                escrow_id=escrow_id,
                shipment=shipment,
                status="PENDING",
                requested_by=settings.RELAYER_ACCOUNT,
                input_payload=payload,
                attestation_note=(
                    "OutLayer TEE (Intel TDX) — genuine attestation; carrier data is SIMULATED "
                    "Demo Carrier. Triggered automatically by the platform relayer."
                ),
            )
        result = _send(
            "request_delivery_verification",
            {"escrow_id": escrow_id, "carrier_payload": json.dumps(payload)},
            deposit=settings.RELAYER_VERIFICATION_DEPOSIT_YOCTO,
        )
        if result["success"]:
            report.requested.append(escrow_id)
        else:
            report.errors.append(f"{escrow_id}: verification request failed: {result['error']}")
        return  # the TEE callback completes asynchronously; continue next tick

    # 2) Sync the attested outcome into the projection.
    if chain.get("last_verification"):
        pending = VerificationRequest.objects.filter(escrow_id=escrow_id, status="PENDING").first()
        if pending:
            sync_verification_from_chain(escrow_id)
            report.synced.append(escrow_id)

    # 3) Pay the logistics allocation once pickup is TEE-verified.
    if (
        chain.get("pickup_verified")
        and not chain.get("logistics_paid")
        and state in ("Shipped", "AwaitingBuyerConfirmation")
    ):
        result = _send("settle_logistics", {"escrow_id": escrow_id})
        if result["success"]:
            report.logistics_paid.append(escrow_id)
        else:
            report.errors.append(f"{escrow_id}: logistics settle failed: {result['error']}")

    # 4) Release after the inspection window, unless a problem was reported.
    if (
        state == "AwaitingBuyerConfirmation"
        and chain.get("delivered_verified")
        and not chain.get("seller_paid")
        and not chain.get("problem_reported")
    ):
        delivered_ms = None
        try:
            delivered_ms = near.view_call(
                settings.VERIPAY_CONTRACT, "delivery_time_ms", {"escrow_id": escrow_id}
            )
        except Exception:  # noqa: BLE001 — pre-upgrade escrows have no clock
            delivered_ms = None
        window_ms = int(chain.get("inspection_period_ms") or 0)
        if delivered_ms and _now_ms() >= delivered_ms + window_ms:
            result = _send("release_after_inspection", {"escrow_id": escrow_id})
            if result["success"]:
                report.released.append(escrow_id)
            else:
                report.errors.append(f"{escrow_id}: release failed: {result['error']}")
