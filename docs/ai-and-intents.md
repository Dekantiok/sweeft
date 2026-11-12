# AI & NEAR Intents infrastructure

Both layers are **provider-neutral**: swapping providers is an environment
change, never a code change. Nothing breaks when a provider is missing —
deterministic fallbacks keep every feature functional offline.

## AI layer (`apps/ai`)

### Interface

```
AIProvider.complete(system, user, json_mode, max_tokens, temperature) -> Completion
```

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

### Services & endpoints

| Service | Endpoint | Purpose | Fallback when provider fails |
|---|---|---|---|
| `parse_deal_text` | `POST /api/v1/ai/parse/` | natural language → deal fields | regex rules (amount, account, days) |
| `recommend_dispute_split` | `POST /api/v1/ai/recommend/` | advisory split, human confirms on-chain | even 50/50 + reason |
| `draft_listing` | `POST /api/v1/ai/listing/` | marketplace listing draft | title from first words, kind guess |
| — | `GET /api/v1/ai/status/` | provider/model/config state (no secrets) | — |

Every response carries `source: <provider|rules>` so the UI can label it honestly.

### Wiring plan (later)

- **Create escrow / listing forms**: "AI fill" button → `/api/v1/ai/parse/` • `/api/v1/ai/listing/`
- **Dispute panel**: "Ask AI arbiter" → `/api/v1/ai/recommend/` → the buyer confirms the
  recorded `pct` on-chain via `ai_recommend` + `resolve_review` (advisory only, always human-signed)
- **Voice**: browser SpeechRecognition → same parse endpoint (no server change)

## NEAR Intents layer (`apps/intents`)

### Interface

```
IntentsProvider.quote(asset_out, amount_out, asset_in) -> Quote
IntentsProvider.status(quote_id) -> dict
```

| Provider | What it is | Config |
|---|---|---|
| `dry` | Offline deterministic quotes, **clearly simulated** (default) | nothing |
| `oneclick` | 1Click (Intent Swap) HTTP API — quote / deposit address / status | `INTENTS_BASE_URL`, `INTENTS_API_KEY` |

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/intents/status/` | provider, dry-run flag, API key presence |
| `POST /api/v1/intents/quote/` | `{escrow_id}` (sized to the on-chain total) or `{asset_out, amount_out}` |
| `GET /api/v1/intents/quotes/<quote_id>/` | poll a quote/deposit |

### Wiring plan (later)

1. Escrow records `foreign_asset` (already in the contract).
2. Buyer picks a source chain → `quote_for_escrow(escrow_id)` → deposit address shown in the UI.
3. Buyer sends from their chain; the 1Click status endpoint confirms settlement.
4. Settlement converts to tUSD → funding continues through the normal `ft_transfer_call` path —
   the escrow contract itself needs **no changes** for the first integration.

**Caveat (honest):** the `oneclick` adapter is implemented against the documented
endpoints but is **not enabled by default and not yet verified against the live
service**. Review <https://docs.near-intents.org> and the current API schema
before flipping `INTENTS_PROVIDER=oneclick`. Until then, `dry` quotes are
labelled `simulated` everywhere they appear.
