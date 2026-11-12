# Architecture

## Components

```
┌────────────┐  wallet-signed txs   ┌──────────────────────┐
│  Browser   │ ───────────────────► │  NEAR testnet        │
│ Django     │    NEP-413 login     │  veripay.*.testnet   │
│ templates  │                      │  (Rust escrow)       │
│ + wallet   │                      └──────┬───────────────┘
│   bundle   │                             │ request_execution
└─────┬──────┘                             ▼
      │ HTTPS                       ┌──────────────────────┐
      ▼                             │  outlayer.testnet    │
┌────────────┐  RPC reads (views)   │  TEE workers (TDX)   │
│  Django    │ ◄──────────────────► │  run our verifier    │
│  backend   │                      └──────────────────────┘
│ SQLite     │
└────────────┘
```

- **Smart contract (Rust, near-sdk 5.x)** — financial and authorization source of truth.
  Holds tUSD, enforces state transitions and permissions, performs payouts via NEP-141
  `ft_transfer`, and is the only path through which TEE verification can be recorded
  (`on_verification_result` is `#[private]`, reachable only via the contract's own promise
  chain back from `outlayer.testnet`).
- **Django (modular monolith)** — product coordination: wallet challenge/verify (NEP-413),
  agreement metadata, invitation locators, partner applications/approval, shipment simulation,
  verification orchestration, marketplace listings + purchase requests, projections and audit
  records. It cannot move funds: payouts are triggered by wallet-signed calls to permissioned
  contract methods.
- **Marketplace (`apps/market`)** — discovery layer only: listings reference seller wallets, trust
  profiles read live contract counters, and "trade via escrow" prefills the on-chain agreement.
  The catalog can be rebuilt by anyone from chain data (`escrows_range`, `reputation_of`).
- **OutLayer** — off-chain verifiable execution. Our verification WASM is published (hash-pinned);
  the contract calls `request_execution`; the TEE result returns through the promise callback.
- **Demo Carrier** — a simulated carrier adapter behind a `CarrierAdapter` interface
  (`apps/logistics/carriers.py`). All its data is flagged `simulated=True` and labelled in the UI.

## Source-of-truth rule

| Concern | Authority |
|---|---|
| Funds, balances, settlement | NEAR contract |
| Authorization / permissions | NEAR contract (mirrored in Django for UX only) |
| Agreement descriptions, invitations | Django (hash/ref committed on-chain) |
| Shipment events | Carrier (real integrations) / Demo Carrier (simulated, labelled) |
| Delivery verification | OutLayer TEE attestation, recorded by the contract callback |

Django must reconcile its projection with contract state and must never treat its database as
authoritative for funds (`POST /api/v1/agreements/<id>/sync/` refreshes the projection from chain).

## Wallet identity

A connected NEAR wallet is the transaction identity. Login is a NEP-413 signed message:
single-use nonce, domain-bound recipient, 10-minute expiry, verified server-side
(ed25519 or secp256k1) and consumed once. No passwords, no emails, no private keys stored.
