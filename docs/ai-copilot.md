# Trade co-pilot (AI agent)

**The agent proposes; the wallet authorizes.** The AI never holds user keys. Every
money-moving step is either completed in the existing wallet modal (`SweftApp`) or —
for co-pilot payments — bounded on-chain by a **native-NEAR budget** the user funds
with a single wallet signature. The budget contract is the security boundary.

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

## Provider layer

Provider-neutral: swapping providers is an environment change, never a code change. Nothing
breaks when a provider is missing — deterministic fallbacks keep every feature functional offline.

| Provider | What it is | Config |
|---|---|---|
| `echo` | Deterministic rules, **no network** (default) | nothing |
| `openai_compat` | Any OpenAI-compatible `/chat/completions` — NEAR AI Cloud, OpenAI, Groq, Together, vLLM, Ollama, LM Studio | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` |
| `anthropic` | Anthropic Messages API | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` |

```bash
# NEAR AI Cloud
AI_PROVIDER=openai_compat
AI_BASE_URL=https://cloud-api.near.ai/v1
AI_API_KEY=...
AI_MODEL=deepseek/deepseek-v4.1-flash
```

**Verified live:** with the NEAR AI Cloud key, `parse_deal_text` returned
`{"product": "solar parts", "amount": 0.25, "beneficiary": "fewcap2636.testnet",
"deadlineDays": 5, "source": "openai_compat"}`.

### Structured endpoints (same fallback rules)

| Service | Endpoint | Fallback when provider fails |
|---|---|---|
| `parse_deal_text` | `POST /api/v1/ai/parse/` | regex rules (amount, account, days) |
| `recommend_dispute_split` | `POST /api/v1/ai/recommend/` | even 50/50 + reason |
| `draft_listing` | `POST /api/v1/ai/listing/` | title from first words, kind guess |
| status | `GET /api/v1/ai/status/` | provider/model/config state (no secrets) |

Every response carries `source: <provider|rules>` so the UI labels it honestly.

The model never touches private keys. The write-side actions are an **off-chain
purchase request** and a **payment intent**; acceptance/funding always require
the user's wallet, and agent payments only settle within the deposited budget.

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `search_listings` | read | ACTIVE listings filtered by text/budget/kind, ranked by seller on-chain reputation |
| `get_seller_score` | read | live `reputation_of` → factual trust summary + rank |
| `check_my_balance` | read | connected wallet's native NEAR balance |
| `get_my_trade_status` | read | open escrows, completed trades, disputes |
| `send_purchase_request` | prepare | files an off-chain purchase request for the seller to act on |
| `prepare_purchase` | prepare | proposal card: seller score, totals (price + logistics + fee), affordability |
| `prepare_funding` | prepare | accept+fund card for an escrow the buyer already owns |
| `prepare_dispute` | prepare | dispute case-file card with the user's reason |
| `prepare_payment` | prepare | co-pilot payment card: pays a NEAR account from the user's native budget |

Every card links into the existing UI (`/market/<slug>/`, `/escrow/<id>/`) — no
second signing path.

## Co-pilot payments (native NEAR budget)

The only money-moving agent capability, bounded twice over:

1. The user deposits exactly the NEAR the agent may spend
   (`deposit()` on `budget.steepdata3968.testnet`). The budget contract tracks
   `allowance`, `spent`, and `available` on-chain.
2. A **per-payment cap is enforced on-chain** by the budget contract
   (`get_max_payment`/`set_max_payment`, default 0.1 NEAR) — the server setting
   (`AI_AGENT_MAX_PAYMENT`) only mirrors it for UX. Over-cap requests are refused, not split.

- Only the configured agent account may call `pay(owner, receiver, amount)`.
- `spent` rolls back through a private callback if the NEAR transfer fails.
- Raise the limit with another deposit; revoke by `withdraw(amount)` of the
  unspent remainder. Setting a new limit is always a wallet-signed user action.
- Every payment is recorded as a `PaymentIntent` (audit) with its tx hash.
- Endpoint: `POST /api/v1/ai/payments/<id>/execute/` — session wallet must own
  the intent; idempotent once executed.

**Testnet disclosure:** the demo agent key (`agent.steepdata3968.testnet`) lives
in the server environment because this is a testnet MVP (see `.env.example`).
On mainnet the same key would run inside a TEE/KMS. The design does not change —
even a fully compromised agent key can never exceed the deposited budget.

## Examples

- “buy solar inverters under 1.5 NEAR” → search → seller record → balance check
  → proposal card with total cost and affordability
- “check my balance” → native NEAR balance reply
- “score for seller.steepdata3968.testnet” → on-chain trust summary
- “dispute SWFT-100185 goods arrived damaged” → dispute proposal card
- “pay 0.025 NEAR to seller.steepdata3968.testnet for invoice 12” → payment card →
  “Pay now” settles from the deposited budget (deposit flow if none yet)
- Real provider (NEAR AI Cloud) verified: `run_agent` returned
  `search_listings` with a natural-language reply.

## Safety rules enforced in code

- Tools act only as the authenticated session wallet — never an AI-supplied address.
- One validated tool call per turn; tool names outside the registry are rejected.
- No autonomous loops, no background purchases, no hidden calls (trace is shown).
- Listings/descriptions are untrusted input for the model (prompt-injection aware).
- Proposal cards always display the exact totals and the buyer's balance.
- Payments: recipient validated, self-payments rejected, per-payment cap enforced
  before signing, remaining budget enforced by the budget contract.

## Fallback (`echo`)

When no provider is configured, a deterministic keyword router performs the same
tools (search, balance, score, dispute, payment) and answers with rule-based text.
The product still works; only the language understanding is reduced.
