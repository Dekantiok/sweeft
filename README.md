# SWEeft — AI-powered decentralised escrow on NEAR (testnet v1)

Problem: freelancers and digital-goods sellers get scammed or stuck in chargeback hell.
Product: escrow where buyer + seller are fixed at creation, both join via escrow code,
buyer funds, payer releases or anyone settles after deadline if no dispute.
AI (NEAR AI Cloud) pre-fills the form from text/voice and recommends on disputes.
Human always confirms. Settlement on NEAR testnet (native NEAR in v1).

UI: landing page is only "Connect wallet"; after connecting there are three tabs —
Create (role toggle buyer/seller + AI pre-fill), Join (code lookup, role-gated actions,
AI arbiter on disputes) and My escrows (wallet-indexed list).
Wallets: Meteor, HERE and Nightly (MyNearWallet is discontinued and not offered).
After connecting, the landing footer shows the running build tag, e.g. `v4-visibility-2026-11-10`.

Visibility model: the escrow code alone does not reveal the escrow. Creating it puts it in
the creator's My escrows immediately; the counterparty sees nothing until they enter the code
and press Join. The contract rejects anyone who is not the buyer/seller fixed at creation.

NEAR role: escrow locked in an on-chain contract, named accounts for party identity,
chain-abstraction ready (`foreign_asset` stub for Intents).

## Try it (do X - see Y)

Prereqs: Docker, testnet accounts, `NEAR_AI_KEY` in `.env` (see `.env.example`).

1. `cp .env.example .env` — fill `NEAR_AI_KEY` (optional: without it the API uses a regex/50-50 fallback)
2. `docker compose up --build` — see web on http://localhost:3000, api on http://localhost:4000/health
3. Do: open web — see: only a "Connect wallet (testnet)" button
4. Do: connect wallet — see: Create / Join / My escrows tabs
5. Do: in Create, type "pay 50 USDC to alice.testnet for logo by Friday" and press AI pre-fill — see: form filled
6. Do: set counterparty + amount, choose role, press Create — see: code `SWFT-XXXX` (random 4-digit) shown
7. Do: open My escrows as the creator — see: the new escrow is listed there already
8. Do: connect the counterparty wallet and open My escrows — see: it is NOT listed yet
9. Do: open code in Join tab as the counterparty, press Join — see: status `joined`; the escrow now appears in their My escrows too
10. Do: buyer presses Deposit — see: status `funded`
11. Do: seller marks delivered, buyer releases — see: status `released`
12. Do: raise dispute, ask AI arbiter, record + confirm payout — see: status `resolved`
13. Do: create with 1-day deadline, edit nothing, press Settle after deadline — see: error before deadline; status `settled` after

You need two testnet wallets to try both sides (buyer + seller), or use
`seller.steepdata3968.testnet` as counterparty and play the buyer only.

## Contract

`contract/` — `near-sdk-js`. States: `created -> joined -> funded -> delivered -> released | disputed -> resolved | settled`.
`settle(id)` callable by anyone after deadline iff `!disputed`. AI key is recommend-only.
Views: `get(id)` and `my_escrows(account_id)` (wallet-indexed list for the My escrows tab).

Contract v3 (2026-11-10) changed the index: `create` adds only the creator, `join` adds the
counterparty, and every escrow records a `creator` field. v3 also moved escrow storage to a
fresh namespace (`e3`), which wipes the earlier test escrows on redeploy.

Deployed testnet account: `steepdata3968.testnet` — live.
Explorer: https://explorer.testnet.near.org/accounts/steepdata3968.testnet
Verified on-chain: v1/v2 flows `SWFT-1` released, `SWFT-2` resolved, `SWFT-3` settled;
v3 visibility: `SWFT-9297` is listed for creator `steepdata3968.testnet` and NOT for
counterparty `fewcap2636.testnet`, `SWFT-6257` is listed for the creator before join and
for `seller.steepdata3968.testnet` only after that wallet joined; a third account calling
`join` on `SWFT-9297` panics with `not a party`.
Query: `near contract call-function as-read-only steepdata3968.testnet my_escrows '{"account_id":"steepdata3968.testnet"}' network-config testnet now`.

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
