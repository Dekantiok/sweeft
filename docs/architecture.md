# Architecture

## Components

```
┌────────────┐  wallet-signed txs   ┌──────────────────────┐
│  Browser   │ ───────────────────► │  NEAR testnet        │
│ Django     │    NEP-413 login     │  sweeft.*.testnet   │
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
  Holds native NEAR (legacy tUSD supported), enforces state transitions and permissions,
  performs payouts via native transfer or NEP-141
  `ft_transfer`, and is the only path through which TEE verification can be recorded
  (`on_verification_result` is `#[private]`, reachable only via the contract's own promise
  chain back from `outlayer.testnet`).
- **Django (modular monolith)** — product coordination: wallet challenge/verify (NEP-413),
  agreement metadata, invitation locators, partner applications/approval, shipment simulation,
  verification orchestration, marketplace listings + purchase requests, projections and audit
  records. It cannot move funds outside the escrow's fixed paths: payouts execute only through
  contract methods whose state-machine conditions and beneficiaries are set at creation —
  triggered by wallet-signed calls, or by the platform relayer's permissionless calls.
- **Relayer (`apps/escrow/relayer.py`, `manage.py run_relayer`)** — platform-side automation:
  requests TEE verification when the carrier reports delivery, syncs the attested result, pays the
  logistics allocation on verified pickup, and releases seller + platform after the inspection
  window (unless a problem was reported). Every call is permissionless but state-machine gated;
  the relayer key can never redirect funds. Testnet MVP: the key lives on the demo server (mainnet:
  TEE/KMS).
- **Marketplace (`apps/market`)** — discovery layer only: listings reference seller wallets, trust
  profiles read live contract counters, and "trade via escrow" prefills the on-chain agreement.
  The catalog can be rebuilt by anyone from chain data (`escrows_range`, `reputation_of`).
- **OutLayer** — off-chain verifiable execution. Our verification WASM is published (hash-pinned);
  the contract calls `request_execution`; the TEE result returns through the promise callback.
- **Demo Carrier** — a simulated carrier. The shipment state machine lives behind a
  `CarrierAdapter` interface (`apps/logistics/carriers.py`, forward-only and idempotent); the
  service a real carrier would run is emulated by a **standalone simulator**
  (`tools/demo-carrier/simulator.py`) that only talks to the partner API over HTTP — no database
  access. All its data is flagged `simulated=True` and labelled in the UI.

## Source-of-truth rule

| Concern | Authority |
|---|---|
| Funds, balances, settlement | NEAR contract |
| Authorization / permissions | NEAR contract (mirrored in Django for UX only) |
| Agreement descriptions, invitations | Django (hash/ref committed on-chain) |
| Shipment events | Carrier (real integrations) / Demo Carrier (simulated, labelled) |
| Delivery verification | OutLayer TEE attestation, recorded by the contract callback (permissionless request — typically the platform relayer) |

Django must reconcile its projection with contract state and must never treat its database as
authoritative for funds (`POST /api/v1/agreements/<id>/sync/` refreshes the projection from chain).

## Wallet identity

A connected NEAR wallet is the transaction identity. Login is a NEP-413 signed message:
single-use nonce, domain-bound recipient, 10-minute expiry, verified server-side
(ed25519 or secp256k1) and consumed once. No passwords, no emails, no private keys stored.
