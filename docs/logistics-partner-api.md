# Logistics partner API

Two surfaces exist:

1. **Partner portal** (wallet-signed, in-browser) — for humans: profile, shipments, simulated scans.
2. **Partner API** (revocable API key) — for automated provider systems. The standalone
   **Demo Carrier simulator** (`tools/demo-carrier/simulator.py`) is the reference consumer.

## Registration & approval

`POST /partners/register/` (form, wallet-signed session) collects company details, coverage,
services, tracking API endpoint and the **settlement wallet** (the on-chain logistics beneficiary).
Applications start as `PENDING`; an admin reviews in Django admin and moves them through
`UNDER_REVIEW → APPROVED → INTEGRATION_PENDING → ACTIVE` (or `SUSPENDED` / `REJECTED`).
Only `ACTIVE` partners appear in the public directory and may be assigned to new agreements.
Suspension blocks new assignments; existing funded agreements continue under their terms.

API credentials are stored **hashed** with a public prefix; rotation creates a new key and
deactivates the old one. Mint one for the demo partner (printed once):

```bash
python3 manage.py seed_demo --issue-carrier-key
```

## Endpoints (key-authenticated)

Authenticate with `X-Api-Key: swft_…`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/partners/shipments/` | shipments assigned to this partner (flagged `simulated`), with their event timeline |
| GET | `/api/v1/partners/shipments/?include_escrow_state=1` | adds `escrow_state` (cached contract read) so a provider can gate its run on actual dispatch |
| POST | `/api/v1/partners/shipments/{id}/events/` | submit a status event `{status, note?}` |

Statuses are validated against the normalized set: `CREATED, ACCEPTED, PICKED_UP, IN_TRANSIT,
OUT_FOR_DELIVERY, DELIVERED, DELIVERY_FAILED, RETURNED, LOST, CANCELLED`. Transitions are
**forward-only** and **idempotent**: re-posting the current status is a no-op, backwards moves
return HTTP 400, and a failure status is terminal. This keeps the event timeline monotonic for
the OutLayer TEE validator. Events are stored with `simulated=True` in the MVP (Demo Carrier).

Each shipment carries a `simulation_scenario` (`delivered` | `failed` | `lost`) chosen at
dispatch; it only steers the Demo Carrier simulator — real partners ignore it.

## Demo Carrier simulator (standalone process)

`tools/demo-carrier/simulator.py` is a separate service that plays the ~60-second delivery
lifecycle against this API — the same surface a real carrier integration would use. It owns no
database state; run it next to `runserver`:

```bash
# terminal 1 — SWEEFT
cd sweeft && python3 manage.py runserver 0.0.0.0:8000

# terminal 2 — the carrier (SIMULATED). Key from `seed_demo --issue-carrier-key`.
SWEEFT_BASE_URL=http://127.0.0.1:8000 DEMO_CARRIER_API_KEY=swft_… \
  python3 tools/demo-carrier/simulator.py
```

Timeline per shipment (scenario `delivered`), starting when the escrow is first seen as
`Shipped`:

| Elapsed | Status | Note |
|---|---|---|
| +5s | `ACCEPTED` | parcel received at the origin facility |
| +15s | `PICKED_UP` | courier picked up from the seller |
| +30s | `IN_TRANSIT` | line-haul to the destination city |
| +45s | `OUT_FOR_DELIVERY` | at the destination hub |
| +60s | `DELIVERED` | handover to the buyer |

`failed` and `lost` scenarios swap the ending (`DELIVERY_FAILED` at +60s; `LOST` at +30s).
The escrow page polls `/api/v1/shipments/by-escrow/{escrow_id}/` and renders the live timeline;
at `DELIVERED` it prompts the connected party to request the TEE verification (wallet-signed —
`carrier_payload` is built by the next endpoint below).

## Wallet-session endpoints (portal)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/shipments/` | register a shipment for a funded escrow (seller/provider); accepts `scenario` |
| GET | `/api/v1/shipments/{id}/` | tracking payload (carrier-adapter normalized) |
| GET | `/api/v1/shipments/by-escrow/{escrow_id}/` | latest shipment + timeline for an escrow (powers the live tracking panel) |
| POST | `/api/v1/shipments/{id}/events/` | advance the Demo Carrier (simulated scan; manual override) |
| POST | `/api/v1/verification/payload/` | build the carrier payload for the on-chain TEE request |
| POST | `/api/v1/verification/sync/` | re-read the escrow and store the on-chain verification result |

## Security notes

- Keys are revocable, stored hashed, and never returned after creation.
- Rate limiting/replay protection for webhooks is future work (polling is the MVP mechanism);
  the demo adapter is not exposed as a public webhook.
- Partner-reported events never trigger payouts by themselves: on-chain settlement requires the
  TEE-verified carrier report; the seller allocation releases on buyer confirmation or, if the
  buyer is silent, after the on-chain inspection window (`release_after_inspection`).
