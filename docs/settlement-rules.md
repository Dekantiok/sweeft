# Settlement rules

## Allocation model

```
total_funded = seller_amount + logistics_amount + platform_fee
platform_fee = seller_amount × platform_fee_bps / 10_000     (default bps = 500 → 5%)
```

All amounts are in tUSD (6 decimals) in the MVP. Network gas is separate and borne by the
transacting wallet. Fees are disclosed before funding and enforced by the contract; changing the
fee bps affects only future agreements.

Example (demo values):

| Allocation | tUSD |
|---|---|
| Product / seller | 850.00 |
| Logistics | 10.00 |
| Platform fee (5%) | 42.50 |
| **Total funded** | **902.50** |

## Milestones

- **Logistics** — eligible once carrier **pickup is TEE-verified**. Never paid merely because a
  tracking number was generated. If pickup never occurs, the allocation remains unpaid and follows
  the refund rules.
- **Seller** — eligible after TEE-verified **delivery** AND explicit **buyer confirmation**.
  A silent buyer never releases funds; a problem report keeps the allocation locked.
- **Platform fee** — collected at completion, in the same settlement as the seller payout.

## Payout destinations

Beneficiaries (seller, logistics provider, platform treasury) are recorded before funding and
enforced by the contract. No backend request can alter them after funding. Payouts are NEP-141
`ft_transfer` calls from the contract; every transfer is idempotency-guarded by `*_paid` flags,
and a failed transfer rolls its flag back so the payout can be retried.

## Failure cases

| Case | Behaviour |
|---|---|
| Seller does not dispatch | buyer claims `refund_buyer` after the dispatch deadline (no shipment registered); logistics forfeits |
| Carrier reports loss/return | verification returns the status; no automatic fault assigned; review path applies |
| Wrong/damaged goods | buyer `report_problem` → `ReviewRequired`; funds locked; disclosed operator split via `resolve_review` |
| Verification unavailable | no fabricated result: the escrow stays `Shipped`; retry or review |
| Duplicate event / payout | rejected by state checks + `*_paid` flags |
| FT receiver not registered | transfer fails, flag rolls back, remains retryable |

## Token boundary

MVP payouts are on-chain transfers to NEAR-compatible wallets in the supported testnet token
(`tusd.steepdata3968.testnet`, open-mint demo token). Fiat/bank payouts require a separate provider,
compliance work and reconciliation — explicitly out of scope.
