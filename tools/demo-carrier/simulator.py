#!/usr/bin/env python3
"""Demo Carrier simulator — a standalone, clearly-simulated logistics service.

Runs the ~60-second delivery lifecycle (receive → transport → deliver) against
VeriPay's carrier Partner API, exactly like a real provider integration would:
HTTP only, API-key authenticated, no shared database, no Django imports.

    python3 manage.py seed_demo --issue-carrier-key        # prints the key once
    VERIPAY_BASE_URL=http://127.0.0.1:8000 \
    DEMO_CARRIER_API_KEY=vp_... \
    python3 tools/demo-carrier/simulator.py

Timeline per shipment (scenario `delivered`), starting when the shipment is
first seen with its escrow dispatched on-chain (`escrow_state == Shipped`):

    +5s   ACCEPTED          parcel received at the origin facility
    +15s  PICKED_UP         courier picked up the parcel from the seller
    +30s  IN_TRANSIT        line-haul transport to the destination city
    +45s  OUT_FOR_DELIVERY  at the destination hub, out for delivery
    +60s  DELIVERED         handover to the buyer

Scenarios `failed` and `lost` swap the ending so dispute flows stay demoable.
Everything posted is simulated carrier data; OutLayer verifies the resulting
tracking report on-chain when a party requests it.

Notes:
- Run state is in memory: if the process restarts mid-run, the remaining
  events replay from a fresh 60s window (posts are idempotent and forward-only
  server-side, so the timeline stays monotonic).
- Shipments registered before dispatch simply wait (gated on `Shipped`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

TERMINAL_STATUSES = {"DELIVERED", "DELIVERY_FAILED", "RETURNED", "LOST", "CANCELLED"}

# (seconds after run start, status, scan note)
SCENARIO_SCHEDULES: dict[str, list[tuple[int, str, str]]] = {
    "delivered": [
        (5, "ACCEPTED", "Parcel received at the origin facility"),
        (15, "PICKED_UP", "Courier picked up the parcel from the seller"),
        (30, "IN_TRANSIT", "Line-haul transport to the destination city"),
        (45, "OUT_FOR_DELIVERY", "Arrived at the destination hub — out for delivery"),
        (60, "DELIVERED", "Delivered to the buyer"),
    ],
    "failed": [
        (5, "ACCEPTED", "Parcel received at the origin facility"),
        (15, "PICKED_UP", "Courier picked up the parcel from the seller"),
        (30, "IN_TRANSIT", "Line-haul transport to the destination city"),
        (45, "OUT_FOR_DELIVERY", "Arrived at the destination hub — out for delivery"),
        (60, "DELIVERY_FAILED", "Delivery attempt failed — recipient unavailable"),
    ],
    "lost": [
        (5, "ACCEPTED", "Parcel received at the origin facility"),
        (15, "PICKED_UP", "Courier picked up the parcel from the seller"),
        (30, "LOST", "Parcel lost in transit — investigation opened"),
    ],
}


def due_events(scenario: str, elapsed_seconds: float, posted_statuses: set[str]) -> list[dict]:
    """Events whose scheduled moment has passed and that were not posted yet."""
    schedule = SCENARIO_SCHEDULES.get(scenario, SCENARIO_SCHEDULES["delivered"])
    return [
        {"status": status, "note": note}
        for at, status, note in schedule
        if elapsed_seconds >= at and status not in posted_statuses
    ]


class VeriPayApi:
    """Thin HTTP client for the carrier Partner API (X-Api-Key auth)."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def shipments(self) -> list[dict]:
        payload = self._request("GET", "/api/v1/partners/shipments/?include_escrow_state=1")
        return payload.get("shipments", [])

    def post_event(self, shipment_id: int, status: str, note: str) -> dict:
        return self._request(
            "POST",
            f"/api/v1/partners/shipments/{shipment_id}/events/",
            {"status": status, "note": note},
        )


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def tick(api: VeriPayApi, shipment: dict, started: dict[str, float]) -> None:
    """Advance one shipment if its scheduled events have come due."""
    tracking = shipment["tracking_number"]
    if shipment["status"] in TERMINAL_STATUSES:
        started.pop(tracking, None)
        return
    if shipment.get("escrow_state") != "Shipped":
        return  # not dispatched yet — wait for the on-chain register_shipment
    t0 = started.setdefault(tracking, time.monotonic())
    elapsed = time.monotonic() - t0
    posted = {event["status"] for event in shipment.get("events", [])}
    for event in due_events(shipment.get("simulation_scenario", "delivered"), elapsed, posted):
        api.post_event(shipment["id"], event["status"], event["note"])
        log(f"{tracking}: {event['status']} — {event['note']}")


def run(base_url: str, api_key: str, poll_seconds: float, once: bool = False) -> None:
    api = VeriPayApi(base_url, api_key)
    started: dict[str, float] = {}
    log(f"Demo Carrier (SIMULATED) up — {base_url}, polling every {poll_seconds:g}s")
    while True:
        try:
            shipments = api.shipments()
            in_flight = sum(1 for s in shipments if s["status"] not in TERMINAL_STATUSES)
            log(f"poll: {len(shipments)} shipment(s), {in_flight} in flight")
            for shipment in shipments:
                tick(api, shipment, started)
        except urllib.error.HTTPError as e:
            log(f"poll failed: HTTP {e.code} — is DEMO_CARRIER_API_KEY valid?")
        except Exception as e:  # keep simulating; the next poll retries
            log(f"poll failed: {e}")
        if once:
            return
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo Carrier simulator (SIMULATED tracking)")
    parser.add_argument("--base-url", default=os.environ.get("VERIPAY_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("DEMO_CARRIER_API_KEY", ""))
    parser.add_argument("--poll", type=float, default=float(os.environ.get("DEMO_CARRIER_POLL_SECONDS", "2")))
    parser.add_argument("--once", action="store_true", help="run a single poll cycle (tests/demos) and exit")
    args = parser.parse_args(argv)

    if not args.api_key:
        print("DEMO_CARRIER_API_KEY is required — mint one with:", file=sys.stderr)
        print("  python3 manage.py seed_demo --issue-carrier-key", file=sys.stderr)
        return 2
    try:
        run(args.base_url, args.api_key, args.poll, once=args.once)
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
