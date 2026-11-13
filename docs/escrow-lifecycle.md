# Escrow lifecycle

## Contract states

```
PendingAcceptance ──accept──► Approved ──fund──► Funded ──assign──► ShipmentPending
        │                        │                 │                      │
     reject                   cancel           refund_buyer*          register
        ▼                        ▼                 ▼                      ▼
    Rejected                 Cancelled          Refunded               Shipped
                                                                        │
                                          request_delivery_verification │ (TEE)
                                                                        ▼
                                                            AwaitingBuyerConfirmation
                                                              │            │
                                                     confirm_receipt   report_problem
                                                              ▼            ▼
                                                          Completed   ReviewRequired
```

\* `refund_buyer` is available after the dispatch deadline when no shipment was registered,
including from `ReviewRequired`. Shipped escrows also have a 30-day long-stop, and unresolved
disputes have a seven-day buyer-refund fallback.

## Step by step

1. **Create** (seller) — `create_escrow(product_name, product_desc_hash, seller_amount,
   logistics_amount, buyer?, invite_hash?, token, dispatch_deadline_ms, inspection_period_ms,
   terms_hash)`. The platform fee is computed from the configured bps (5%) and enforced.
   Case A: `buyer` preassigned (must accept). Case B: `invite_hash = sha256(invitation token)`;
   the link is only a locator — acceptance is still a wallet-signed transaction.
2. **Accept** (buyer) — `accept_escrow(escrow_id, invite_secret?)`. The first valid invitee
   presenting the secret becomes the buyer in Case B.
3. **Fund** (buyer) — payable `fund_escrow(escrow_id)` attaches the exact native-NEAR total.
   The contract validates the buyer, amount and `Approved` state; duplicates are rejected.
4. **Assign provider** (seller) — `assign_logistics_provider(escrow_id, provider)` confirms the
   provider committed at creation, or fixes the first provider if none was committed. It cannot
   replace an existing beneficiary.
5. **Register shipment** (seller or provider) — `register_shipment(escrow_id, tracking_number)`.
   Registering a tracking number is NOT proof of pickup and unlocks nothing by itself.
6. **Verify delivery** (seller, provider or buyer) — `request_delivery_verification(escrow_id,
   carrier_payload)` with a NEAR deposit for OutLayer compute. The contract forwards the request
   to `outlayer.testnet`; the TEE result is returned to `on_verification_result`, which records
   `pickup_verified` (for PICKED_UP…DELIVERED) and `delivered_verified` + state
   `AwaitingBuyerConfirmation` (for DELIVERED).
7. **Settle logistics** (seller or provider) — allowed once pickup is verified; pays the recorded
   provider in native NEAR.
8. **Confirm receipt** (buyer) — requires `delivered_verified` and no open problem. Pays any due
   logistics allocation, then the seller and platform fee through chained callbacks. Alternatively,
   anyone can call `release_after_inspection` after the displayed inspection deadline. Failed
   transfers roll the relevant flags back; reputation moves only after the seller payout succeeds.
9. **Report problem** (buyer or seller) — state `ReviewRequired`; funds stay locked. The review
   board's `resolve_review(seller_pct)` proposal splits the remaining balance, including the platform
   fee and unpaid logistics share. After seven days without resolution the buyer can claim the
   remaining balance; no board action is then required.

## Authorization summary

| Action | Authorized party |
|---|---|
| Create agreement | seller (any wallet creating it) |
| Accept / reject | preassigned buyer, or first invitee presenting the secret |
| Fund | the accepted buyer, exact total, native NEAR |
| Assign/confirm immutable provider | seller |
| Register shipment | seller or the assigned provider |
| Request verification | permissionless; state and deposit gated |
| Record verification | **only** the contract's own callback from OutLayer |
| Settle logistics | permissionless after verified pickup |
| Confirm receipt / report problem | buyer (seller may also open a dispute) |
| Claim refund | buyer (dispatch, long-stop or dispute deadline) |
| Resolve review | review board only |
