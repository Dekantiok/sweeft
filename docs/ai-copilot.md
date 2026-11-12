# Trade co-pilot (AI agent)

**The agent proposes; the wallet authorizes.** The AI never holds keys and never
signs financial transactions. Every money-moving step completes in the existing
wallet modal (`VPApp`), exactly as if the user had clicked the buttons.

## Architecture

```
floating 💬 panel (every page)
  → POST /api/v1/ai/chat/ {message}
  → provider (echo | openai_compat | anthropic) returns {"reply", "action"}
  → server validates tool name + args
  → tool executes (read-only data, or an ACTION CARD)
  → panel renders reply + tool trace + cards
  → user clicks a card → the normal wallet flow signs it
```

The model never touches private keys. The only write-side action available to
the agent is an **off-chain purchase request**; acceptance and funding are always
signed by the user.

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

Every card links into the existing UI (`/market/<slug>/`, `/escrow/<id>/`) — no
second signing path.

## Examples

- “buy solar inverters under 1500 tUSD” → search → seller record → balance check
  → proposal card with total cost and affordability
- “check my balance” → balance reply
- “score for seller.steepdata3968.testnet” → on-chain trust summary
- “dispute VP-000006 goods arrived damaged” → dispute proposal card
- Real provider (NEAR AI Cloud) verified: `run_agent` returned
  `search_listings` with a natural-language reply.

## Safety rules enforced in code

- Tools act only as the authenticated session wallet — never an AI-supplied address.
- One validated tool call per turn; tool names outside the registry are rejected.
- No autonomous loops, no background purchases, no hidden calls (trace is shown).
- Listings/descriptions are untrusted input for the model (prompt-injection aware).
- Proposal cards always display the exact totals and the buyer's balance.

## Fallback (`echo`)

When no provider is configured, a deterministic keyword router performs the same
tools (search, balance, score, dispute) and answers with rule-based text. The
product still works; only the language understanding is reduced.
