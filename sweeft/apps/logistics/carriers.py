"""Carrier adapter interface + Demo Carrier (SIMULATED).

Every piece of data produced by DemoCarrier is simulated and is labelled as
such in the database (`simulated=True`), API responses and the UI. It is not a
real carrier integration and never presented as one.

The durable 60-second delivery run lives in the standalone simulator
(`tools/demo-carrier/simulator.py`), which drives the Partner API exactly like
a real carrier integration. This adapter owns the state machine: forward-only,
idempotent (re-posting the current status never duplicates events) so the
timeline OutLayer validates stays monotonic even if a client retries.
"""
from __future__ import annotations

import hashlib
import time

from django.conf import settings

from .models import Shipment, ShipmentEvent


def canonical_carrier_message(payload: dict) -> str:
    """Canonical signing message — MUST match the TEE verifier byte-for-byte.

    `v1|provider_id|tracking_number|shipment_id|status|issued_at|<event statuses, joined>`
    Statuses are raw payload strings, in payload order.
    """
    statuses = ",".join(
        str((event or {}).get("status", "")) for event in (payload.get("events") or [])
    )
    issued_at = payload.get("issued_at", 0)
    return (
        f"v1|{payload.get('provider_id', '')}|{payload.get('tracking_number', '')}"
        f"|{payload.get('shipment_id', '')}|{payload.get('status', '')}|{issued_at}|{statuses}"
    )


def sign_carrier_payload(payload: dict) -> dict:
    """Attach the Demo Carrier service's ed25519 signature when configured.

    The matching public key is registered on-chain (`set_carrier_pubkey`), so
    the TEE verifier only accepts carrier data signed by this key — a caller
    can no longer fabricate a payload from public ids alone.
    """
    seed_hex = getattr(settings, "DEMO_CARRIER_PRIVATE_KEY", "") or ""
    if not seed_hex:
        raise ValueError("Demo Carrier signing key is not configured — refusing to create an unsigned payload")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    payload["issued_at"] = int(time.time() * 1000)
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    payload["signature"] = key.sign(canonical_carrier_message(payload).encode()).hex()
    return payload

HAPPY_FLOW = [
    "CREATED",
    "ACCEPTED",
    "PICKED_UP",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "DELIVERED",
]

FAILURE_STATUSES = {"DELIVERY_FAILED", "RETURNED", "LOST", "CANCELLED"}
TERMINAL_STATUSES = FAILURE_STATUSES | {"DELIVERED"}

ALL_STATUSES = {choice[0] for choice in Shipment.STATUS_CHOICES}


class CarrierAdapter:
    """Interface every carrier integration must implement."""

    def quote(self, origin: str, destination: str, weight_kg: float) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def create_shipment(self, escrow_id: str, origin: str, destination: str) -> dict:  # pragma: no cover
        raise NotImplementedError

    def tracking(self, tracking_number: str) -> dict:  # pragma: no cover
        raise NotImplementedError


class DemoCarrier(CarrierAdapter):
    """Deterministic simulated carrier for the MVP demo."""

    name = "Demo Carrier (simulated)"
    provider_id = "LOG-DEMO-001"

    def quote(self, origin: str, destination: str, weight_kg: float) -> dict:
        base = 10_000_000_000_000_000_000_000  # 0.01 NEAR in yoctoNEAR
        price = base + int(weight_kg * 1_000_000_000_000_000_000_000)  # +0.001 NEAR/kg
        return {
            "carrier": "demo",
            "simulated": True,
            "origin": origin,
            "destination": destination,
            "weight_kg": weight_kg,
            "price": str(price),
            "currency": "NEAR",
            "note": "SIMULATED QUOTE — Demo Carrier, not a real carrier rate.",
        }

    def create_shipment(self, escrow_id: str, origin: str, destination: str) -> dict:
        digest = hashlib.sha256(f"{escrow_id}:{origin}:{destination}".encode()).hexdigest()[:8].upper()
        tracking = f"DEMO-{digest}"
        return {"tracking_number": tracking, "simulated": True, "status": "CREATED"}

    def tracking(self, tracking_number: str) -> dict:
        shipment = Shipment.objects.filter(tracking_number=tracking_number).first()
        if not shipment:
            return {"simulated": True, "error": "unknown tracking number"}
        events = list(shipment.events.values("status", "created_at", "note"))
        return {
            "simulated": True,
            "provider_id": self.provider_id,
            "carrier": "demo",
            "tracking_number": shipment.tracking_number,
            "status": shipment.status,
            "events": [
                {"status": e["status"], "timestamp": e["created_at"].isoformat(), "note": e["note"]}
                for e in events
            ],
            "note": "SIMULATED TRACKING — Demo Carrier. Not a real carrier integration.",
        }

    def advance(self, shipment: Shipment, to_status: str | None = None, note: str = "") -> ShipmentEvent:
        """Advance the simulated shipment one step (or jump to `to_status`).

        Rules (protects the OutLayer-validated timeline):

        - re-posting the current status is a no-op: the existing event is
          returned and nothing is duplicated;
        - happy-chain statuses may only move forward;
        - a failure status may be posted from any non-terminal state and is
          terminal (nothing may follow it).

        Raises ``ValueError`` for unknown statuses and illegal transitions;
        callers translate that into HTTP 400.
        """
        current = shipment.status
        if to_status is None:
            try:
                idx = HAPPY_FLOW.index(current)
            except ValueError:
                idx = -1
            to_status = HAPPY_FLOW[idx + 1] if idx + 1 < len(HAPPY_FLOW) else current
        to_status = to_status.strip().upper()

        if to_status not in ALL_STATUSES:
            raise ValueError(f"unknown status {to_status!r} — one of {sorted(ALL_STATUSES)}")

        # Idempotent re-post: return the existing event, do not duplicate it.
        if to_status == current:
            existing = shipment.events.order_by("-created_at").first()
            if existing and existing.status == current:
                return existing
            # Fall through: the status was set without an event ever existing.

        if current in TERMINAL_STATUSES:
            raise ValueError(f"shipment is already {current} — no further transitions")

        if (
            to_status not in FAILURE_STATUSES
            and current in HAPPY_FLOW
            and HAPPY_FLOW.index(to_status) < HAPPY_FLOW.index(current)
        ):
            raise ValueError(f"cannot move backwards: {current} → {to_status}")

        shipment.status = to_status
        shipment.save(update_fields=["status", "updated_at"])
        return ShipmentEvent.objects.create(
            shipment=shipment,
            status=to_status,
            note=note or "Simulated scan by Demo Carrier",
            simulated=True,
            raw={"simulated": True, "source": "demo_carrier.advance", "previous": current},
        )


def get_carrier() -> DemoCarrier:
    return DemoCarrier()
