# Trade co-pilot (AI agent)

**The agent proposes; the wallet authorizes.** The AI never holds user keys. Every
money-moving step is either completed in the existing wallet modal (`VPApp`) or —
for co-pilot payments — bounded on-chain by an **allowance** the user grants with
a single wallet signature. The allowance is the security boundary, not key custody.

## Architecture

```
floating 💬 panel (every page)
  → POST /api/v1/ai/chat/ {message}
  → provider (echo | openai_compat | anthropic) returns {"reply", "action"}
  → server validates tool name + args
  → tool executes (read-only data, an ACTION CARD, or a payment intent)
  → panel renders reply + tool trace + cards
  → user clicks a card → the normal wallet flow signs it
```

The model never touches private keys. The write-side actions are an **off-chain
purchase request** and a **payment intent**; acceptance/funding always require
the user's wallet, and payments only settle within the user-granted allowance.

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `search_listings` | read | ACTIVE listings filtered by text/budget/kind, ranked by seller on-chain reputation |
| `get_seller_score` | read | live `reputation_of` → factual trust summary + rank |
| `check_my_balance` | read | connected wallet's tUSD balance |
| `get_my_trade_status` | read | open escrows, completed trades, disputes |
| `send_purchase_request` | prepare | files an off-chain purchase request for the seller to act on |
| `prepare_purchase` | prepare | proposal card: seller score, totals (price + logistics + fee), affordability |
| `prepare_funding` | prepare | accept+fund card for an escrow the buyer already owns |
| `prepare_dispute` | prepare | dispute case-file card with the user's reason |
| `prepare_payment` | prepare | co-pilot payment card: pays a NEAR account in tUSD from the user-granted budget |

Every card links into the existing UI (`/market/<slug>/`, `/escrow/<id>/`) — no
second signing path.

## Co-pilot payments (allowance-scoped)

The only money-moving agent capability, and it is bounded twice over:

1. The user grants the co-pilot a tUSD **allowance** with ONE wallet-signed
   `approve(spender, amount)` on the token. The token contract itself enforces
   the budget for every `ft_transfer_from` the agent signs.
2. The platform enforces a **per-payment cap** (`AI_AGENT_MAX_PAYMENT`, default
   100 tUSD) before signing anything. Over-cap requests are refused, not split.

- Revoke any time by approving `0`; remaining allowance is visible on-card.
- Failed transfers roll the allowance deduction back (same transaction).
- Every payment is recorded as a `PaymentIntent` (audit) with its tx hash.
- Endpoint: `POST /api/v1/ai/payments/<id>/execute/` — session wallet must own
  the intent; idempotent once executed.

**Testnet disclosure:** the demo agent key (`agent.steepdata3968.testnet`) lives
in the server environment because this is a testnet MVP (see `.env.example`).
On mainnet the same key would run inside a TEE/KMS. The design does not change —
even a fully compromised agent key can never exceed the granted allowance.

## Examples

- “buy solar inverters under 1500 tUSD” → search → seller record → balance check
  → proposal card with total cost and affordability
- “check my balance” → balance reply
- “score for seller.steepdata3968.testnet” → on-chain trust summary
- “dispute VP-000006 goods arrived damaged” → dispute proposal card
- “pay 25 tUSD to seller.steepdata3968.testnet for invoice 12” → payment card →
  “Pay now” settles from the granted budget (grant flow if none yet)
- Real provider (NEAR AI Cloud) verified: `run_agent` returned
  `search_listings` with a natural-language reply.

## Safety rules enforced in code

- Tools act only as the authenticated session wallet — never an AI-supplied address.
- One validated tool call per turn; tool names outside the registry are rejected.
- No autonomous loops, no background purchases, no hidden calls (trace is shown).
- Listings/descriptions are untrusted input for the model (prompt-injection aware).
- Proposal cards always display the exact totals and the buyer's balance.
- Payments: exact-1-yocto deposits, recipient validated, self-payments rejected,
  per-payment cap enforced before signing, allowance enforced by the token contract.

## Fallback (`echo`)

When no provider is configured, a deterministic keyword router performs the same
tools (search, balance, score, dispute, payment) and answers with rule-based text.
The product still works; only the language understanding is reduced.
