# SWEeft — AI-powered decentralised escrow on NEAR (testnet v1)

Problem: freelancers and digital-goods sellers get scammed or stuck in chargeback hell.
Product: escrow where buyer + seller are fixed at creation, both join via escrow code,
buyer funds, payer releases or anyone settles after deadline if no dispute.
AI (NEAR AI Cloud) pre-fills the form from text/voice and recommends on disputes.
Human always confirms. Settlement on NEAR testnet NEP-141 USDC.

NEAR role: escrow locked in on-chain contract, stablecoin rails, meta-txs so
non-crypto users can use it, chain-abstraction ready (`foreignAsset` stub for Intents).

## Try it (do X - see Y)

Prereqs: Docker, testnet accounts, `NEAR_AI_KEY` in `.env` (see `.env.example`).

1. `cp .env.example .env` — fill `NEAR_AI_KEY`, contract account
2. `docker compose up --build` — see web on http://localhost:3000, api on http://localhost:4000/health
3. Do: open web, connect wallet (testnet) — see: wallet address in header
4. Do: type "pay 50 USDC to alice.testnet for logo by Friday" in AI box — see: form pre-filled
5. Do: create escrow with fixed seller — see: escrow code + explorer link
6. Do: join as counterparty, buyer deposits — see: status `funded`
7. Do: submit delivery, payer releases — see: status `released`
8. Do: create with 10-min deadline, wait, call Settle with no dispute — see: status `settled`

## Contract

`contract/` — `near-sdk-js`. States: `created -> joined -> funded -> delivered -> released | disputed -> resolved | settled`.
`settle(id)` callable by anyone after deadline iff `!disputed`. AI key is recommend-only.

Deployed testnet account: `steepdata3968.testnet` (deploy with `ESCROW_CONTRACT=steepdata3968.testnet bash contract/deploy.sh`).

## API

`apps/api` — stateless proxy to NEAR AI Cloud. No keys in repo.

- `GET /health`
- `POST /parse` `{text}` -> `{amount, token, beneficiary, deadlineDays, terms}`
- `POST /recommend` `{evidence, terms}` -> `{pct, reason}`

## Roadmap (not v1)

- Telegram bot (same API, voice-native)
- Opt-in auto-release under cap with pre-sign
- Real Intents cross-chain (EVM -> NEAR), currently `dry:true` only

## Hackathon

Built at NEAR HACKATHON. Baseline: repo created 2026-11-09, no prior NEAR code.
License: MIT.
