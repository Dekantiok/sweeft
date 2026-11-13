# Marketplace & reputation

The marketplace is the discovery layer of the SWEEFT ecosystem. It never touches funds:
trading always happens through the on-chain escrow, and **one wallet** connects listings,
profiles, escrows and logistics.

```
Browse listings → check seller's on-chain trust profile → request to buy
→ seller issues the escrow (prefilled) → buyer accepts + funds
→ delivery TEE-verified → buyer confirms → reputation moves for both sides
```

## Reputation (on-chain, source of truth)

The contract keeps a `Reputation` record per wallet, updated **only inside existing state
transitions** — never on creation:

| Counter | Moves when |
|---|---|
| `completed_as_seller` | a funded escrow reaches `Completed` with this wallet as seller |
| `completed_as_buyer` | …as buyer |
| `volume` | sum of seller amounts over completed deals (both sides) |
| `disputes_opened` | the buyer files `report_problem` (stays neutral until resolved) |
| `refunds_claimed` | the buyer claims a dispatch-deadline refund |
| `requests_rejected` | the buyer declines an agreement |
| `first_seen_ms` | first contract interaction |

Views: `reputation_of(account)`, `reputations([...])`, plus `escrows_range(start, limit)` and
`escrow_count()` so **anyone can re-index the whole book from chain data alone** — the Django
database is a cache, never the truth.

## Marketplace (Django, off-chain catalog)

- `Listing` — seller wallet, title, kind (goods/service), category, description, price (native NEAR),
  location, photo URLs, default dispatch/inspection windows. Status: active / paused / sold.
- `PurchaseRequest` — a buyer asks the seller to issue an escrow. The contract fixes the seller
  as creator (a buyer cannot open an escrow in someone else's name), so the seller issues it with
  one click; both sides still sign on-chain.
- Pages: `/market/` (browse + search), `/market/<slug>/`, `/market/new/`, `/market/mine/`.
- **Trust profile** `/u/<wallet>/` — live contract reputation, active listings, escrow history,
  explorer link.
- **Trade via escrow** — prefills the agreement (product, price, dispatch/inspection windows) and,
  on success, marks the listing `sold` and closes the pending purchase request.

## Anti-gaming honesty

- Counters move only on **completed funded cycles**; creating agreements earns nothing.
- Wash-trading is cheap on testnet — the mechanism is complete, but reputation is
  **economically unproven** at this stage (stated plainly in the README).
- Volume is recorded, so a wallet with one loud trade is distinguishable from a steady history.
- Disputes are neutral until resolved; `REVIEW_REQUIRED` never counts as a win or loss.
- Every number shown on the profile is recomputable from the contract — the UI reads it live
  and labels the source.
