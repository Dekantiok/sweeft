#!/usr/bin/env python3
"""Print a signed Demo Carrier payload for an escrow id (TEE-ready).

Usage: python3 scripts/carrier_payload.py VP-000001
"""
import json
import os
import sys

BASE = os.path.join(os.path.dirname(__file__), "..", "sweeft")
sys.path.insert(0, BASE)
os.chdir(BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from apps.escrow import near  # noqa: E402
from apps.logistics import carriers  # noqa: E402

escrow_id = sys.argv[1]
chain = near.get_escrow(escrow_id) or {}
payload = {
    "provider_id": os.environ.get("CARRIER_PROVIDER_ID", "LOG-DEMO-001"),
    "shipment_id": chain.get("shipment_id"),
    "tracking_number": chain.get("tracking_number"),
    "status": "DELIVERED",
    "events": [{"status": "PICKED_UP"}, {"status": "DELIVERED"}],
}
print(json.dumps(carriers.sign_carrier_payload(payload)))
