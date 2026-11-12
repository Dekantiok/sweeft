# Logistics partner API

Two surfaces exist:

1. **Partner portal** (wallet-signed, in-browser) — for humans: profile, shipments, simulated scans.
2. **Partner API** (revocable API key) — for automated provider systems.

## Registration & approval

`POST /partners/register/` (form, wallet-signed session) collects company details, coverage,
services, tracking API endpoint and the **settlement wallet** (the on-chain logistics beneficiary).
Applications start as `PENDING`; an admin reviews in Django admin and moves them through
`UNDER_REVIEW → APPROVED → INTEGRATION_PENDING → ACTIVE` (or `SUSPENDED` / `REJECTED`).
Only `ACTIVE` partners appear in the public directory and may be assigned to new agreements.
Suspension blocks new assignments; existing funded agreements continue under their terms.

API credentials are stored **hashed** with a public prefix; rotation creates a new key and
deactivates the old one.

## Endpoints (key-authenticated)

Authenticate with `X-Api-Key: vp_…`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/partners/shipments/` | shipments assigned to this partner (flagged `simulated`) |
| POST | `/api/v1/partners/shipments/{id}/events/` | submit a status event `{status, note?}` |

Statuses are validated against the normalized set: `CREATED, ACCEPTED, PICKED_UP, IN_TRANSIT,
OUT_FOR_DELIVERY, DELIVERED, DELIVERY_FAILED, RETURNED, LOST, CANCELLED`. Events are processed
idempotently by status and stored with `simulated=True` in the MVP (Demo Carrier).

## Wallet-session endpoints (portal)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/shipments/` | register a shipment for a funded escrow (seller/provider) |
| GET | `/api/v1/shipments/{id}/` | tracking payload (carrier-adapter normalized) |
| POST | `/api/v1/shipments/{id}/events/` | advance the Demo Carrier (simulated scan) |
| POST | `/api/v1/verification/payload/` | build the carrier payload for the on-chain TEE request |
| POST | `/api/v1/verification/sync/` | re-read the escrow and store the on-chain verification result |

## Security notes

- Keys are revocable, stored hashed, and never returned after creation.
- Rate limiting/replay protection for webhooks is future work (polling is the MVP mechanism);
  the demo adapter is not exposed as a public webhook.
- Partner-reported events never trigger payouts by themselves: on-chain settlement requires the
  TEE-verified carrier report and, for the seller allocation, explicit buyer confirmation.
