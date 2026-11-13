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

## Notes / follow-ups

- The buyer-dispute state gate is intentional (disputes open at `Shipped`); sellers can
  escalate earlier from `Funded`/`ShipmentPending` — this run exercised both.
- The public `rpc.testnet.near.org` endpoint now returns “deprecated” throttling; the app
  uses `https://test.rpc.fastnear.com` (set in `.env`).
