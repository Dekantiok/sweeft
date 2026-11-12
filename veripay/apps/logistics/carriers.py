"""Carrier adapter interface + Demo Carrier (SIMULATED).

Every piece of data produced by DemoCarrier is simulated and is labelled as
such in the database (`simulated=True`), API responses and the UI. It is not a
real carrier integration and never presented as one.
"""
from __future__ import annotations

import hashlib

from .models import Shipment, ShipmentEvent

HAPPY_FLOW = [
    "CREATED",
    "ACCEPTED",
    "PICKED_UP",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "DELIVERED",
]


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
        base = 800_000  # 0.80 tUSD in token units (6 decimals)
        price = base + int(weight_kg * 120_000)
        return {
            "carrier": "demo",
            "simulated": True,
            "origin": origin,
            "destination": destination,
            "weight_kg": weight_kg,
            "price": str(price),
            "currency": "tUSD",
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
        """Advance the simulated shipment one step (or jump to `to_status`)."""
        current = shipment.status
        if to_status is None:
            try:
                idx = HAPPY_FLOW.index(current)
            except ValueError:
                idx = -1
            to_status = HAPPY_FLOW[idx + 1] if idx + 1 < len(HAPPY_FLOW) else current
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
