# Verified: end-to-end on NEAR testnet

Current deployment: escrow **`sweeft-market.testnet`**, review board **`sweeft-board.testnet`**
(both keyless/immutable — deploy keys deleted), TEE project
**`steepdata3968.testnet/sweeft-verify`**, carrier signing key registered on-chain.

## Lifecycle — `SWFT-611104` → `Completed`

| Step | Tx |
|---|---|
| `create_escrow` (seller) | `AAYvJ8n67kiHQn8npHYdfpTa25JobebqGDCSfuEJeULr` |
| `accept_escrow` (buyer) | `HgDZ7D8nPkmYPxgbFzwYBGBsScKJjW3h1A2TvRkRJ8BY` |
| `fund_escrow` 0.115 NEAR | `2mZvSAk99funVe3gUXDU4wXpVVLoo8pxzBqtNPhJUYC2` |
| `assign_logistics_provider` | `BDb8wr3wySVrya4xvaY29MpU4wNu9kLQWfnGEMAxRycK` |
| `register_shipment` | `8VcvAAM1LexgfS7SXatuSgdg1R4mdRwBas6J6kscfvwz` |
| TEE verification (signed carrier payload) | `BEjCyuthDpMwL53obGY62C751aNQkjGYeZAUda81STxo` |
| `settle_logistics` | `45ULkRNqd3Ya3LwEyxLTm8BLy95mphPTGGq6Ygwcqa5s` |
| `confirm_receipt` | `CorSpp82LnTaqL1wSKXLXAev3Ff1mAFvjgG1ZzA8texY` |

Final state `Completed`; seller, logistics and platform payout flags all true. Payout allocations:
seller **0.1 NEAR**, logistics **0.01 NEAR**, platform fee **0.005 NEAR** to the board treasury.
The signed carrier result verified successfully. Reputation is written after the seller payout.

## Marketplace regression — `SWFT-951559` → `Refunded`

| Step | Result / tx |
|---|---|
| Create with provider and tracking id committed | `9ykb1W5NBq6giD5Q8ifYmRXBCwmgBzd6uhTufDH9L6zU` |
| Accept | `fhXZedw8ZfcjLRsmytSurLuhr3vL8Pk63zw3mK8GQXi` |
| Fund 0.115 NEAR | `EfGLW1qNyJqkNazregzydMcn2b4jioeXM7Qtmc2BaKQ5` |
| Confirm original provider | `3ddXpcUf1XicnBp3M1VQACp1VERWnFS82dG3fmoW8d89` |
| Before dispatch | state `ShipmentPending`, `shipment_registered=false` |
| Buyer refunds after dispatch deadline | `B8HT4jcxXTmLghMRig9d1zQWNr4xHyBtMeEuGf15o2vC` |

Final state `Refunded`; the tracking id remained committed but was never treated as dispatch. No
logistics/seller/platform allocation was paid, and refund reputation moved after the transfer.

## Deployment

- Account creation: `FNEMvKecLwv6u51pmckfeeGQA6ZacdiF8kRcR4awMHci`
- WASM deploy: `3bhnYWAsrjaSh5fHaSVGikphopVpEwvYQAr5ooWV5yRK`
- Initialization: `Lg1qqAojTLWTwA4c17XhrERYoxHTYicHusfb3fMTinp`
- Board treasury configuration: `5H3f1m9CdqhhfTduwPVPYcEWoZuFESZ8gdoMAnTwXBAr`
- Escrow deploy-key deletion: `HsuYFphPiJE9wL3Ye8LRTBkpWrCSMgBmLpGFKm7utFNN`
- Deployed and locally built WASM SHA-256 both:
  `063e3b67b486a4ff5c000ab2c0c1f87c3a7cddda1c81604881e93fb0983d1574`
- `view_access_key_list` on `sweeft-market.testnet` returns an empty list.
- Review board `sweeft-board.testnet` remains keyless and council-governed at 2-of-3.

## Regression coverage

The 62 escrow unit tests cover beneficiary immutability, creation-time tracking, no-dispatch and
long-stop refunds, dispute deadlines, failed settlement/refund rollback, resolution retries with
paid and unpaid logistics, storage caps, carrier identity, inspection timing and post-payout
reputation. Legacy-pool stakes cannot be claimed during a pending seller payout, and a successful
buyer fallback refund finalizes the case so remaining stakes are claimable.

| Attempt | On-chain result |
|---|---|
| Provider replacement after funding | reverted: `logistics provider is fixed before funding` |
| `vote_dispute` / `finalize_arbitration` with a review board set | reverted: `arbitration pool is disabled while a review board is set` |
| Owner retaking the board via `set_arbiter(owner)` | reverted: `board already set — only the current board can change it` |
| `withdraw(amount, to)` | `MethodNotFound` — method removed |
| `create_escrow` without a storage deposit | reverted: `attach at least 0.02 NEAR storage deposit` |
| `create_escrow` with an unlisted token | reverted: `token not allowed` |
