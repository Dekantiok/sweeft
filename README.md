# SWEeft — AI-powered decentralised escrow on NEAR (testnet v1)

Problem: freelancers and digital-goods sellers get scammed or stuck in chargeback hell.
Product: escrow where buyer + seller are fixed at creation, both join via escrow code,
buyer funds, payer releases or anyone settles after deadline if no dispute.
AI (NEAR AI Cloud) pre-fills the form from text/voice and recommends on disputes.
Human always confirms. Settlement on NEAR testnet (native NEAR in v1).

UI: landing page is only "Connect wallet"; after connecting there are three tabs —
Create (role toggle buyer/seller + AI pre-fill), Requests (incoming accept/reject inbox + ID lookup),
and My escrows (wallet-indexed list).
Wallets: Meteor, HERE and Nightly (MyNearWallet is discontinued and not offered).
Link an email to your wallet from the header so others can invite you by email.
After connecting, the landing footer shows the running build tag, e.g. `v8-requests-inbox-2026-11-11`.

Discovery model: no codes to share. The creator types the counterparty's wallet or linked email;
the counterparty finds the escrow under Requests and accepts (join) or rejects. Accepting lists it
in their My escrows; rejecting marks it `rejected`, visible to both sides. `SWFT-XXXX` IDs remain
as the on-chain reference and stay look-up-able. The contract rejects anyone who is not the
buyer/seller fixed at creation.

NEAR role: escrow locked in an on-chain contract, named accounts for party identity,
chain-abstraction ready (`foreign_asset` stub for Intents).

## Try it (do X - see Y)

Prereqs: Docker, testnet accounts, `NEAR_AI_KEY` in `.env` (see `.env.example`).

1. `cp .env.example .env` — fill `NEAR_AI_KEY` (optional: without it the API uses a regex/50-50 fallback)
2. `docker compose up --build` — see web on http://localhost:3000, api on http://localhost:4000/health
3. Do: open web — see: only a "Connect wallet (testnet)" button
4. Do: connect wallet — see: Create / Join / My escrows tabs
5. Do: in Create, type "pay 50 USDC to alice.testnet for logo by Friday" and press AI pre-fill — see: form filled
6. Do: set counterparty (wallet or linked email) + amount, choose role, press Create — see: request `SWFT-XXXX` sent
7. Do: open My escrows as the creator — see: the new escrow is listed there already
8. Do: connect the counterparty wallet and open Requests — see: the pending request with Accept / Reject
9. Do: link an email in the header (+ Add email) — see: chip with the address; others can now invite this email
10. Do: press Accept as the counterparty — see: status `joined`; the escrow now appears in their My escrows too
11. Do: buyer presses Deposit — see: status `funded`
12. Do: buyer presses Release immediately (no delivery, no deadline wait) — see: status `released`, seller paid
13. Do: raise dispute, ask AI arbiter, record + confirm payout — see: status `resolved`
14. Do: create with 1-day deadline, press Settle after deadline — see: error before deadline; status `settled` after; or press Reject on a fresh request — see: status `rejected`, visible to both sides

You need two testnet wallets to try both sides (buyer + seller), or use
`seller.steepdata3968.testnet` as counterparty and play the buyer only.

## Contract

`contract/` — `near-sdk-js`. States: `created -> joined -> funded -> delivered -> released | disputed -> resolved | settled`, plus `rejected` (declined pre-funding).
`settle(id)` callable by anyone after deadline iff `!disputed`. AI key is recommend-only.
`release(id)` is instant for the buyer once funded — no delivery step and no deadline wait required.
Views: `get(id)`, `my_escrows(account_id)`, `incoming(account_id)` (pending requests),
`email_account(email)` and `account_email(account_id)` (wallet↔email directory).

Contract v4 (2026-11-11) added the request flow: `link_email` links a wallet to an email,
`create` puts a pending entry in `incoming` instead of indexing the counterparty, `join`
accepts, `reject` declines pre-funding (creator can also cancel). Storage moved to a fresh
namespace (`e4`), wiping earlier test escrows.

Deployed testnet account: `steepdata3968.testnet` — live.
Explorer: https://explorer.testnet.near.org/accounts/steepdata3968.testnet
Verified on-chain: v1/v2 flows `SWFT-1` released, `SWFT-2` resolved, `SWFT-3` settled;
v3 visibility `SWFT-9297` creator-only, `SWFT-6257` listed after join, stranger `join` panics `not a party`.
Real payouts: `SWFT-1764` release moved the full 0.01 NEAR deposit to the seller,
`SWFT-5496` settle moved 0.002 NEAR, `SWFT-2229` resolve at 70% paid the seller +0.007 and refunded the buyer +0.003.
(Resolve uses a chained promise — returning parallel joint promises is prohibited on-chain.)
v4 requests: `seller@sweeft.test` linked to `seller.steepdata3968.testnet`; `SWFT-3940`
appeared in `incoming` but not in `my_escrows`, reject cleared it to `rejected`;
`SWFT-2372` accepted, funded, and released instantly with the deadline 24h away (seller +0.001 exact).
Query: `near contract call-function as-read-only steepdata3968.testnet incoming '{"account_id":"seller.steepdata3968.testnet"}' network-config testnet now`.

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
