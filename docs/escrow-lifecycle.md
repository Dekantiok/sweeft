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

\* `refund_buyer` requires the dispatch deadline to have passed AND no shipment registered.

## Step by step

1. **Create** (seller) — `create_escrow(product_name, product_desc_hash, seller_amount,
   logistics_amount, buyer?, invite_hash?, token, dispatch_deadline_ms, inspection_period_ms,
   terms_hash)`. The platform fee is computed from the configured bps (5%) and enforced.
   Case A: `buyer` preassigned (must accept). Case B: `invite_hash = sha256(invitation token)`;
   the link is only a locator — acceptance is still a wallet-signed transaction.
2. **Accept** (buyer) — `accept_escrow(escrow_id, invite_secret?)`. The first valid invitee
   presenting the secret becomes the buyer in Case B.
3. **Fund** (buyer) — NEP-141 `ft_transfer_call` with `msg = escrow_id` and `amount = total`.
   `ft_on_transfer` validates token contract, buyer, exact amount, state `Approved`, and
   rejects duplicates by state; anything invalid is returned to the sender (FT refund).
4. **Assign provider** (seller) — `assign_logistics_provider(escrow_id, provider)`.
5. **Register shipment** (seller or provider) — `register_shipment(escrow_id, tracking_number)`.
   Registering a tracking number is NOT proof of pickup and unlocks nothing by itself.
6. **Verify delivery** (seller, provider or buyer) — `request_delivery_verification(escrow_id,
   carrier_payload)` with a NEAR deposit for OutLayer compute. The contract forwards the request
   to `outlayer.testnet`; the TEE result is returned to `on_verification_result`, which records
   `pickup_verified` (for PICKED_UP…DELIVERED) and `delivered_verified` + state
   `AwaitingBuyerConfirmation` (for DELIVERED).
7. **Settle logistics** (seller or provider) — allowed once pickup is verified; pays the recorded
   provider via `ft_transfer`.
8. **Confirm receipt** (buyer) — requires `delivered_verified` and no open problem. Pays the
   seller, then the platform fee (chained callback). A failed transfer rolls the flags back
   (`on_payout_result`) so it can be retried — the contract never reports a payment that failed.
9. **Report problem** (buyer) — state `ReviewRequired`; funds stay locked. The disclosed operator
   path `resolve_review(seller_pct)` splits the remaining balance; it is a centralized fallback,
   not arbitration.

## Authorization summary

| Action | Authorized party |
|---|---|
| Create agreement | seller (any wallet creating it) |
| Accept / reject | preassigned buyer, or first invitee presenting the secret |
| Fund | the accepted buyer, exact total, supported token |
| Assign provider | seller |
| Register shipment | seller or the assigned provider |
| Request verification | any party to the escrow |
| Record verification | **only** the contract's own callback from OutLayer |
| Settle logistics | seller or provider (after verified pickup) |
| Confirm receipt / report problem | buyer |
| Claim refund | buyer (deadline + no shipment) |
| Resolve review | platform owner (disclosed manual path) |
