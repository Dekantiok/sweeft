# Verified: two-wallet end-to-end (browser + on-chain)

Run on 2026-11-13 with two independent Meteor testnet wallets:

- **Seller / platform**: `steepdata3968.testnet`
- **Buyer**: `fewcap2636.testnet`

Both sessions were signed in through the app's NEP-413 login, verified against on-chain
access keys, and driven in a real browser (Chromium). On-chain steps were executed with the
wallets' own keys; the UI was asserted and screenshotted at every stage.

## 1. Native-NEAR lifecycle — `VP-000020` → `Completed`

| Step | Tx |
|---|---|
| `create_escrow` (seller) | `CWcDuVBD32foZjk5vKpHWwxWWgwQueRaKcAeNtryXUvK` |
| `accept_escrow` (buyer) | `4JF3eNSa5wXs7Z9NJ3tLLaFV3vLb14LrAtohF46qsUQe` |
| `fund_escrow` 0.115 NEAR (buyer) | `7maN4NBWT6CDgzTid6JeCiLrzTuJsZPu4qwJqZRG7qx4` |
| `assign_logistics_provider` (seller) | `7cjXuA8nxtuhYqBRY7ZveJjxKfcJ7wcmT6ek7KsdA5TH` |
| `register_shipment` (seller) | `8tFZ5EbiwNMnJhfEzbtEoxHcBfQYZLoAziD73ZTpYKXp` |
| OutLayer TEE verification (delivered) | callback recorded `verified: true` |
| `settle_logistics` | `8zDiAsz276RN4h7v5KAXQJCFrScmtXDZkLt2EvWbJ9uY` |
| `confirm_receipt` (buyer) | `Am4mhYJhBNHdPc3yFUtGH9cMepRH9tFFLVeLFKxknFMC` |

Final state `Completed`; seller, logistics and platform payout flags all true.

## 2. Dispute → review-board resolution — `VP-000021` → `Refunded`

- Buyer-funded escrow (0.115 NEAR); the **buyer tried to dispute while `Funded` and was
  correctly rejected on-chain** (`buyer cannot dispute in this state`) — the buyer dispute
  window opens at `Shipped`.
- Seller opened the dispute (`9FELVeLeAFUzDVG1X1wc5Qx6rVeMVUZgGuDbBXGyQUah`) →
  `ReviewRequired`.
- Board proposal **#5** (`resolve_review`, 60% to seller) approved by
  `seller.steepdata3968.testnet` + `logistics.steepdata3968.testnet` (the disputing
  seller's account recused itself) → DAO executed the split automatically.
- Result: **Refunded** — seller +0.069 NEAR, buyer +0.046 NEAR (60/40 of 0.115; the
  proposer's 0.1 NEAR bond also returned).

## 3. AI co-pilot payment from the native budget (browser)

- `steepdata3968` deposited into `budget.steepdata3968.testnet` (allowance 0.08 total).
- In the browser chat: *“pay 0.01 NEAR to fewcap2636.testnet for two-wallet E2E”* →
  `Pay now — co-pilot settles it` → **Paid ✓ tx `ANcYNSsa4N8S…`** (server-side agent
  signature, no wallet popup needed).
- On-chain deltas: buyer **+0.01 NEAR**, budget `spent` 0.015 → **0.025**, no other
  balance moved.

## 4. UI assertions (both sessions)

- Landing gating: unauthenticated sees one connect CTA; connected seller reveals the two
  paths via “Start trading”.
- Seller dashboard lists `VP-000020` (Completed) and `VP-000021` (Refunded); escrow page
  shows native amounts, acceptance policy, TEE verification record.
- Buyer dashboard lists both escrows as purchases; escrow page renders `Refunded` with the
  60/40 split; trust profile loads.
- Governance console shows the proposals and approvals; co-pilot chat renders the payment
  card and status with zero JS errors across all pages.

## 5. Post-review hardening verification (2026-11-13, contract v10)

Exploit paths from the pre-submission review, rejected live on `veripay.steepdata3968.testnet`:

| Attempt | On-chain result |
|---|---|
| `vote_dispute` / `finalize_arbitration` with a review board set (seller-sockpuppet path) | reverted: `arbitration pool is disabled while a review board is set` |
| Owner retaking the board via `set_arbiter(owner)` | reverted: `board already set — only the current board can change it` |
| `withdraw(amount, to)` (owner drain) | `MethodNotFound` — method removed |
| `create_escrow` without a storage deposit | reverted: `attach at least 0.02 NEAR storage deposit` |
| `create_escrow` with an unlisted token | reverted: `token not allowed` |

Hardened end-to-end: **`VP-000022`** completed the full lifecycle through the new TEE verifier
(active hash `e5b1a7aa…`) with all payout flags; **`VP-000023`** exercised the tampered-provider
rejection path (contract-side identity checks + verifier unit tests; OutLayer executions were
intermittently timing out at the RPC layer during the run — the positive path above is the
authoritative TEE proof).

## 6. Second review round — fixes and verification (2026-11-13, contract v11, verifier v3)

- **Stranded logistics fix**: `resolve_review`/`finalize_arbitration` computed the split pot
  *after* flipping `logistics_paid`, subtracting the logistics share from the split and locking it
  in the contract. The pot is now computed from the pre-flip state, so an unpaid allocation is
  genuinely folded into the split. Regression tests assert `seller_part + buyer_part == total_amount`
  for both the board and pool paths (`dispute_split_with_unpaid_logistics_covers_the_full_total`,
  `arbitration_split_with_unpaid_logistics_covers_the_full_total`).
- **Carrier signatures (the real TEE fix)**: the Demo Carrier service signs the canonical payload
  (`v1|provider|tracking|shipment|status|events`, ed25519) and the TEE verifier requires a valid
  signature whenever a carrier pubkey is registered on-chain (`set_carrier_pubkey`).
  - `VP-000024`: signed payload → TEE `verified: true`, full lifecycle Completed.
  - `VP-000025`: tampered signature → TEE `verified: false — carrier signature invalid`,
    escrow stayed `Shipped` (no state advance).
- **Fresh-deploy safety**: `deploy_testnet.sh` now **requires `BOARD_ACCOUNT`** and calls
  `set_arbiter` (plus `set_provider_id`/`set_carrier_pubkey`) before finishing — a fresh
  deployment can no longer sit exploitable without a board.
- **Demo-council labelling**: README and the governance UI now state that the 2-of-3 council is a
  demo council operated by this project on testnet.
- OutLayer compute refunds accumulate unspent by design (no owner sweep exists); documented as dust.

## Notes / follow-ups

- The buyer-dispute state gate is intentional (disputes open at `Shipped`); sellers can
  escalate earlier from `Funded`/`ShipmentPending` — this run exercised both.
- The public `rpc.testnet.near.org` endpoint now returns “deprecated” throttling; the app
  uses `https://test.rpc.fastnear.com` (set in `.env`).
