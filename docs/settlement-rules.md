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
- **Seller** — eligible after TEE-verified **delivery** AND either explicit **buyer confirmation**
  or expiry of the inspection window without a problem report. The window starts at the first
  TEE-verified delivery and is at least one hour; the escrow page shows its release time.
- **Platform fee** — collected at normal completion, in the same settlement as the seller payout.
  A dispute instead splits the entire remaining pot, including the fee and any unpaid logistics
  allocation, between buyer and seller; the platform earns no separate fee on that path.

## Payout destinations

Beneficiaries (seller, logistics provider, platform treasury) are enforced by the contract. A
logistics provider committed at creation can only be confirmed after funding; if none was committed,
the seller's first assignment fixes it. No later call can replace it. Payouts are native NEAR
transfers; every transfer is idempotency-guarded by `*_paid` flags, and a failed transfer rolls its
flag back so it can be retried. A failed buyer refund restores the exact pre-refund state.
Completion and refund reputation are recorded only after the corresponding transfer succeeds.

## Failure cases

| Case | Behaviour |
|---|---|
| Seller does not dispatch | buyer claims `refund_buyer` after the dispatch deadline (no shipment registered), even if the seller opened a dispute; logistics forfeits |
| Carrier reports loss/return | verification returns the status; no automatic fault assigned; review path applies |
| Wrong/damaged goods | buyer `report_problem` → `ReviewRequired`; review-board split via `resolve_review`; unresolved disputes default to a buyer refund after 7 days |
| Verification unavailable | no fabricated result: the escrow stays `Shipped`; retry, review, long-stop refund after 30 days, or dispute refund after 7 days in review |
| Duplicate event / payout | rejected by state checks + `*_paid` flags |

## Token boundary

MVP payouts are native NEAR transfers to NEAR-compatible wallets. Fiat/bank payouts require a
separate provider, compliance work and reconciliation — explicitly out of scope.
