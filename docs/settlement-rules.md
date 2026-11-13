# Settlement rules

## Allocation model

```
total_funded = seller_amount + logistics_amount + platform_fee
platform_fee = seller_amount × platform_fee_bps / 10_000     (default bps = 500 → 5%)
```

Amounts are in **native NEAR (yoctoNEAR, 24 decimals)**. Network gas is separate and borne by the transacting wallet. Fees
are disclosed before funding and enforced by the contract; changing the fee bps affects only future
agreements.

Example (native demo values):

| Allocation | NEAR |
|---|---|
| Product / seller | 0.10 |
| Logistics | 0.01 |
| Platform fee (5%) | 0.005 |
| **Total funded** | **0.115** |

## Milestones

- **Logistics** — eligible once carrier **pickup is TEE-verified**. Never paid merely because a
  tracking number was generated. If pickup never occurs, the allocation remains unpaid and follows
  the refund rules.
- **Seller** — eligible after TEE-verified **delivery** AND explicit **buyer confirmation**.
  A silent buyer never releases funds; a problem report keeps the allocation locked.
- **Platform fee** — collected at completion, in the same settlement as the seller payout.

## Payout destinations

Beneficiaries (seller, logistics provider, platform treasury) are recorded before funding and
enforced by the contract. No backend request can alter them after funding. Payouts are native NEAR
transfers; every transfer is
idempotency-guarded by `*_paid` flags, and a failed transfer rolls its flag back so it can be retried.

## Failure cases

| Case | Behaviour |
|---|---|
| Seller does not dispatch | buyer claims `refund_buyer` after the dispatch deadline (no shipment registered); logistics forfeits |
| Carrier reports loss/return | verification returns the status; no automatic fault assigned; review path applies |
| Wrong/damaged goods | buyer `report_problem` → `ReviewRequired`; funds locked; review-board split via `resolve_review` |
| Verification unavailable | no fabricated result: the escrow stays `Shipped`; retry or review |
| Duplicate event / payout | rejected by state checks + `*_paid` flags |

## Token boundary

MVP payouts are native NEAR transfers to NEAR-compatible wallets. Fiat/bank payouts require a
separate provider, compliance work and reconciliation — explicitly out of scope.
