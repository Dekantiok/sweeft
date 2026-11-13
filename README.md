# SWEEFT

**Wallet-based escrow, logistics verification, settlement — and a reputation marketplace — for physical commerce on NEAR testnet.**

Buyers do not send money to an unknown seller before goods ship. Sellers know the buyer funded the
deal. Logistics gets paid only when carrier pickup is TEE-verified; the seller releases on explicit
buyer confirmation — or, if the buyer stays silent, when the on-chain inspection window elapses. The
platform fee is disclosed before funding, and the platform relayer runs this pipeline with no wallet
signatures in the happy path.

One wallet connects the whole ecosystem: **marketplace listings → on-chain trust profile → escrow →
verified logistics → settlement → reputation**. Every reputation number is contract-derived and
recomputable by anyone — the marketplace is discovery, the chain is the truth.

```
CONNECT WALLET → BROWSE / LIST → CHECK TRUST PROFILE → ESCROW
→ ACCEPT → FUND (native NEAR) → SHIP → TEE-VERIFY (AUTOMATIC)
→ SETTLE (seller + logistics + platform) → REPUTATION MOVES
```

Built at NEAR Hackathon (Nov 2026).

---

## What is real vs simulated

| Component | Status |
|---|---|
| Escrow + authorization + settlement | **Real** — Rust contract on NEAR testnet (`sweeft-pay.testnet`) |
| Funding currency | **Native NEAR** — payable `fund_escrow`; no token mint, import, or registration |
| Wallet identity + authorization | **Real** — NEP-413 signature + on-chain access-key ownership verified server-side; every financial action traces to a signed transaction |
| Delivery verification | **Real TEE + carrier signatures** — OutLayer (Intel TDX) runs our published verifier; the carrier report must be **ed25519-signed by the Demo Carrier service**, whose public key is registered on-chain (`set_carrier_pubkey`), so callers cannot forge carrier data from public ids. The verifier additionally enforces shipment id, provider id, timeline and pickup-before-delivery. Carrier data itself remains simulated Demo Carrier |
| Verification & settlement automation | **Real on-chain** — the platform relayer (`manage.py run_relayer`) triggers permissionless TEE verification, the logistics payout, and the inspection-window release; the contract state machine gates every call and fixes every destination/amount |
| Carrier tracking data | **SIMULATED** — Demo Carrier; labelled `SIMULATED DATA` in the DB (`simulated=True`), API responses and UI |
| Platform fee | **Real** — 5% of seller amount, enforced by the contract, paid at completion |
| AI co-pilot payments | **Real on-chain** — scoped, revocable native-NEAR budget (`budget.steepdata3968.testnet`); the agent pays only within the user's deposited limit |
| Dispute arbitration pool | **Disabled** — no deployment with a review board exposes it (sockpuppet-votable, no slashing); disputes resolve ONLY through the board |
| Review board (Sputnik DAO) | **Real, demo council** — disputes resolve ONLY via council-approved on-chain proposals (2-of-3). On testnet all three council members are operated by this project; production would use independent members with hardware/multisig keys |
| Treasury (DAO multisig) | **Real** — platform fees accrue to the board account; spending requires a council-approved Transfer proposal |
| Fiat payouts / real carriers | **Not implemented** (out of MVP scope) |

Boundaries we state plainly: a carrier scan does not prove parcel contents; the buyer can confirm
receipt early or report a problem at any time; a silent buyer's funds release only after the on-chain
inspection window — and a reported problem freezes them into the review path.

## Verified end-to-end on testnet

**Current deployment:** escrow contract [`sweeft-pay.testnet`](https://testnet.near.org/accounts/sweeft-pay.testnet),
review board `sweeft-board.testnet` (both keyless/immutable — deploy keys deleted), TEE project
`steepdata3968.testnet/sweeft-verify`, carrier key registered on-chain. Escrow ids are random
six-digit codes: `SWFT-XXXXXX`. **`SWFT-374253`** completed the full lifecycle on this deployment —
signed-carrier TEE verification (first poll), logistics settled **+0.01 NEAR**, seller **+0.1 NEAR**,
platform fee **+0.005 NEAR** to the board treasury, all payout flags true, and post-settlement
reputation incremented exactly once for each party:

| `SWFT-374253` step | Tx |
|---|---|
| `create_escrow` (seller) | `76mtZFGTKUGxiASXdmhMA2P7wPXpSJCTcKCb6gd4D1vr` |
| `accept_escrow` (buyer) | `CvJr2CmyUqaBfYFpPG6UVMnYs4kSMcB7J5zxok8pfGaE` |
| `fund_escrow` 0.115 NEAR | `GoB9D3NRAih5JNYs3MiaTGJx98Z8cptYHiGjZhd3TfsU` |
| `assign_logistics_provider` | `EsJfnrqb1TjKzHQ57jmpKZ2Ls4k48GoyPG7Vpvfa9mP5` |
| `register_shipment` | `5mK2AtGdaKgBKvzeYvLAXe3NkJLWv5RcxaDgp4RwZrRF` |
| TEE verification (signed carrier payload) | `6WyNXvRGtwq9Eea9LawZms8YTrLKrX1AJM4oaKXuKmb4` |
| `settle_logistics` | `J6i6b4Gx6EcxdkSXMWEgneZBtC5fXn3fbNnhane3o7LJ` |
| `confirm_receipt` (buyer) | `6ohfAex6Pid6k1z55pQFj3bGhUhmHBFGM8FscTiG4wZs` |

The live security probe **`SWFT-563283`** also proved that replacing a committed provider reverts
(`C4nTu67CaQrGVUVqhwCzTs1q5tzxdHqUnuoibQoKyfNY`), while the original provider can be confirmed.
After the seller opened a dispute (`8D3SfdDpTjsZdeMoZSfxiYuZZfN4mVPHhxVMajtyLTJR`), the buyer
claimed the no-dispatch refund (`2vkxDjV4EAPdyj4sFudGPVd5uwJfkuJ8SabPAzqAd3RN`); the final state
is `Refunded` and refund reputation moved after payout. Deploy key deletion:
`GPA8AfRemcEgnkVCL8CtwZJ6AcHaT18Q5yfeqwbdJkDg`; the deployed WASM SHA-256 is
`2a0e388507a3ea001442d66dc2e58dc43c2abae6990bfcc446b312b28067f1ba`.

### Governance (Sputnik DAO review board)

Board: [`sweeft-board.testnet`](https://testnet.near.org/accounts/sweeft-board.testnet) — council 2-of-3
(`steepdata3968` + `seller.steepdata3968` + `logistics.steepdata3968`; a demo council operated by this
project on testnet). Disputes resolve only through council-approved `resolve_review` proposals; platform
fees accrue to the board treasury, and spending requires a council-approved Transfer proposal.

The escrow contract's `get_gov_info` shows `arbiter`/`treasury` = the DAO; the platform owner can appoint
the board but **cannot resolve disputes** — that power is gone by design.

## Architecture

```
contracts/sweeft/     Rust (near-sdk) escrow contract — funds & authorization source of truth
contracts/near-budget/ Native-NEAR co-pilot budget (deposit / agent pay / withdraw)
wasi/sweeft-verify/   TEE verification program → wasm32-wasip2, published on OutLayer
sweeft/               Django modular monolith (product coordinator + projection)
  apps/wallets/        NEP-413 challenge/verify, sessions
  apps/escrow/         agreements, invitations, contract reads, settlement projection, relayer
  apps/logistics/      partners, Demo Carrier, shipments, verification records
  apps/market/         listings, purchase requests, trust profiles (/u/<wallet>/)
  apps/ai/             provider-neutral AI (echo | openai_compat | anthropic) + co-pilot payments — see docs/ai-copilot.md
  templates/ static/   Django templates + HTMX + Alpine + Tailwind; wallet bundle
tools/web-bundle/      esbuild bundle of @near-wallet-selector (Meteor/HERE/Nightly)
tools/demo-carrier/    standalone Demo Carrier simulator — SIMULATED, partner-API only
scripts/               deploy + end-to-end scripts
docs/                  architecture, lifecycle, settlement rules, verification, partner API
```

**Source-of-truth rule:** the contract controls funds and permissions; Django coordinates the
experience and keeps a searchable projection; carrier systems report events; OutLayer attests to the
execution of the configured verification code; the buyer's confirmation remains a separate condition.

## Getting test NEAR

Escrows, marketplace prices, logistics fees, platform fees, and co-pilot
budgets all use **native NEAR**. There is no project token to mint, import, or register.

- **Already have testnet NEAR?** Connect your wallet and go — the header chip shows your native balance.
- **Fresh wallet?** Click **“Get test NEAR”** in the header or dashboard to open the public
  [NEAR testnet faucet](https://near-faucet.io/). Any testnet wallet works.

New escrows pass `token: "near"` and fund with the payable `fund_escrow` method.

## Setup

```bash
# 1. Django app (Python 3.10+, Django 5)
cd sweeft
python3 -m pip install django django-environ   # already present on the dev machine
cp .env.example .env                            # testnet addresses are pre-filled
python3 manage.py migrate
python3 manage.py createsuperuser               # for partner approvals at /admin/
python3 manage.py runserver 0.0.0.0:8000
# → http://localhost:8000
```

Environment variables (`.env`, never commit real values):

| Variable | Meaning |
|---|---|
| `SWEEFT_CONTRACT` | deployed escrow account |
| `AI_BUDGET_CONTRACT` | native-NEAR co-pilot budget account |
| `OUTLAYER_CONTRACT` / `OUTLAYER_PROJECT` | OutLayer receiver + published verification project |
| `RELAYER_ENABLED` / `RELAYER_ACCOUNT` / `RELAYER_PRIVATE_KEY` | platform relayer (see below) |
| `NEAR_NETWORK`, `NEAR_RPC_URL`, `DJANGO_DEBUG`, `DJANGO_SECRET_KEY` | standard config |

### Run the verification relayer (second terminal)

The relayer (`apps/escrow/relayer.py`) drives the **permissionless** happy path on-chain: TEE
verification of the carrier report → logistics payout → inspection-window release. It calls only
methods whose destinations and amounts are fixed by the escrow, so the key can never redirect
funds. Fund a dedicated testnet account and set it in `.env`:

```bash
# in sweeft/.env
RELAYER_ENABLED=1
RELAYER_ACCOUNT=relayer.<your>.testnet
RELAYER_PRIVATE_KEY=ed25519:…
RELAYER_POLL_SECONDS=5              # optional

python3 manage.py run_relayer       # loop; or --once for a single pass / cron
```

Without it, the escrow page keeps manual fallback buttons (wallet-signed) — the flow degrades
gracefully. Testnet MVP only: on mainnet the key would live inside a TEE/KMS.

### Contracts

```bash
rustup target add wasm32-unknown-unknown wasm32-wasip2
cargo install cargo-near          # or grab the prebuilt release

(cd contracts/sweeft && cargo near build non-reproducible-wasm --no-abi)
(cd contracts/near-budget && cargo near build non-reproducible-wasm --no-abi)

# deploy + init (see scripts/deploy_testnet.sh for the full flow)
near contract deploy <acct> use-file target/near/sweeft.wasm without-init-call ...
near contract call-function as-transaction <acct> new json-args '{...}' ...
```

### Publish the TEE verifier (OutLayer)

```bash
curl -fsSL https://raw.githubusercontent.com/out-layer/outlayer-cli/main/install.sh | sh
# (or: cargo install outlayer-cli)
outlayer login testnet
(cd wasi/sweeft-verify && cargo build --target wasm32-wasip2 --release)
outlayer upload wasi/sweeft-verify/target/wasm32-wasip2/release/sweeft-verify.wasm
outlayer deploy sweeft-verify <FastFS URL printed by upload>
```

### Frontend bundle (only if you change wallet code)

```bash
cd tools/web-bundle && npm install && npm run build   # writes sweeft/static/js/sweeft-wallet.js
```

## Deploy on Render

The repo ships a [Render blueprint](render.yaml): a Python web service plus a managed Postgres,
with WhiteNoise serving static files and migrations applied during the build.

1. Push this repo to GitHub, then in Render choose **New + → Blueprint** and pick the repo.
2. Apply — Render creates the database, generates `DJANGO_SECRET_KEY`, and wires `DATABASE_URL`,
   allowed hosts and TLS cookies. It prompts for the optional `sync: false` secrets
   (carrier / co-pilot / relayer keys); paste them from `sweeft/.env` or leave blank.
3. Open the service URL — the footer shows the live contract account.

Free-plan notes: the web service spins down when idle (the first request warms it up), and the
free Postgres is fine for the demo. Nothing secret lives in the repo — all keys come from env vars.

## Demo workflow

1. Connect a wallet (Meteor / HERE / Nightly — sign the free NEP-413 message to sign in).
2. **Marketplace**: browse `/market/`, open a listing, inspect the seller's **trust profile**
   (`/u/<wallet>/` — live contract reputation), then "Request to buy via escrow".
3. The seller sees the purchase request under `/market/mine/` and creates the escrow — prefilled
   from the listing — with a wallet-signed `create_escrow`. When creating directly, the seller
   explicitly picks the acceptance policy: **only a specific wallet** (the contract rejects every
   other account) or **an invite link** (only its sha256 is committed on-chain).
4. Buyer accepts, then **funds** the exact total in native NEAR. Logistics is assigned and the Demo
   Carrier tracking number is registered.
5. **Dispatch starts the carrier run**: at "Mark dispatched" pick a scenario (`delivered` in ~60s,
   or `failed` / `lost` for dispute demos). The standalone Demo Carrier simulator plays
   receive → transport → deliver over the partner API; the escrow page shows the live timeline.
6. **The platform relayer runs the settlement pipeline automatically** (start it once:
   `python3 manage.py run_relayer`): when the carrier reports delivery it requests the OutLayer TEE
   verification; when the attested result lands it pays logistics; after the inspection window it
   releases seller + platform — **no wallet signatures in the happy path**. The buyer can confirm
   receipt early, or **report a problem** at any moment: that freezes the funds into the review path.
7. With settlement complete the listing flips to `sold` and **reputation moves on-chain for both
   wallets** — visible instantly on their profiles. Platform fees land in the **review-board treasury**.
8. **Governance**: while an escrow is `ReviewRequired`, either party can **Escalate to the review board**
   from the escrow page — a wallet-signed DAO proposal (`resolve_review`); council members approve on
   `/governance/`; at 2-of-3 the DAO executes the split on-chain. The arbitration pool is
   **disabled in every deployment with a board** (it was exploitable via sockpuppet voting and
   has no slashing); the review board is the only resolution path.

Scripted version of the escrow path: `scripts/e2e_lifecycle.sh`.

### Demo Carrier simulator (SIMULATED — separate process)

The carrier is a **standalone service**, not part of Django: it talks only to the key-authenticated
partner API (the same surface a real GIG/DHL/FedEx integration would use) and holds no database
state. Start it next to `runserver`:

```bash
python3 manage.py seed_demo --issue-carrier-key          # one-time: prints the swft_… key
SWEEFT_BASE_URL=http://127.0.0.1:8000 DEMO_CARRIER_API_KEY=swft_… \
  python3 tools/demo-carrier/simulator.py
```

It gates on the on-chain `Shipped` state, then POSTs scans at +5s `ACCEPTED`, +15s `PICKED_UP`,
+30s `IN_TRANSIT`, +45s `OUT_FOR_DELIVERY`, +60s `DELIVERED` (per-shipment scenario may instead
end `DELIVERY_FAILED` at +60s or `LOST` at +30s). Server-side transitions are idempotent and
forward-only, so retries and restarts cannot corrupt the timeline OutLayer validates. If the
simulator is down, shipments wait at `CREATED` and the UI says so — nothing else breaks.

### AI co-pilot payments (native NEAR budget, user-set limit)

Open the floating 💬 panel and talk to the co-pilot, e.g.
“pay 0.025 NEAR to seller.steepdata3968.testnet for invoice 12”.

1. The co-pilot prepares a payment card (recipient, amount, your available budget).
2. **You set the limit** — type the exact NEAR amount on the card (or the dashboard “Co-pilot budget”
   card) and deposit it with **one wallet signature**. “Pay now” settles instantly once a budget exists.
3. The agent then calls `pay(owner, receiver, amount)` on `budget.steepdata3968.testnet` — only the
   agent can call it, and only within `allowance - spent`. Raise the limit with another deposit;
   revoke by withdrawing the unspent remainder.

Platform policy caps each payment (`AI_AGENT_MAX_PAYMENT`, default 0.1 NEAR).
Everything is audited as a `PaymentIntent` with its tx hash. Testnet disclosure:
the demo agent key lives in the server environment for the MVP; on mainnet the
same design runs with the key inside a TEE/KMS — the on-chain budget stays the boundary.

Verified live: deposited 0.05 NEAR → co-pilot paid native invoices; `available` decreased exactly.
Over-cap and no-budget paths refused correctly.

### Governance console

- `/governance/` — board account, council members + threshold, all proposals with votes and statuses,
  treasury balance, treasury Transfer proposals, and the on-chain wiring
  (`arbiter`/`treasury`).
- Escrow page — “Review board” panel appears while a dispute is open: escalation form for the parties,
  Approve/Reject for council members, live vote chips.
- Contract — `get_gov_info`, `set_arbiter`, `set_treasury` (+ 7 Rust unit tests for the authorization
  rules) and permissionless `retry_platform_payout` for failed fee transfers.

## Security & trust boundaries (post-review hardening)

A pre-submission review surfaced real exploit paths; all critical findings are fixed and verified
on-chain (transaction evidence in `docs/verified-two-wallet-e2e.md`):

| Finding | Fix |
|---|---|
| Seller sockpuppet arbitration (open dispute → fake vote → settle → reclaim stake) | The arbitration pool is **disabled whenever a review board is set** (`vote_dispute`/`finalize_arbitration` revert); the board is the only resolution path. Votes are one-per-wallet and board resolutions finalize open cases so stakes stay claimable. Tie-break is buyer-favourable. **`deploy_testnet.sh` now requires `BOARD_ACCOUNT` and calls `set_arbiter` before anything else**, so a fresh deployment is never left exploitable. |
| Owner could drain escrowed NEAR via `withdraw` | **Removed.** No owner sweep exists; value leaves only through settlement/board paths. |
| Owner could retake dispute power via `set_arbiter(owner)` | `set_arbiter`/`set_treasury` are **bootstrap-only for the owner**; afterwards only the current board/treasury can rotate them. Verified live: owner call reverts. |
| Double payouts from failed buyer legs / folded logistics | Independent per-leg settlement: the buyer remainder is stored and retried via permissionless `retry_review_buyer_payout`; resolution paths mark folded logistics paid; `settle_logistics` is state-gated; reputation counters are exactly-once and value-filtered. |
| Storage-drain spam / unbounded input | Creation requires a **0.02 NEAR storage deposit**; string lengths, per-account indexes and pending lists are capped; escrow count is capped; token field is an **allowlist** (native NEAR). |
| TEE input controlled by caller | **Carrier signatures are mandatory**: the carrier payload must be ed25519-signed by the key registered on-chain (`set_carrier_pubkey`, required at init); the TEE verifier rejects unsigned/invalid payloads (it fails closed when no key is configured). The contract further enforces tracking + carrier-provider identity, and the verifier enforces shipment identity, timeline and pickup-before-delivery. |
| Logistics allocation could strand | Completion (`confirm_receipt` **and** the timeout release) now pays the logistics allocation if still due — requires a recorded provider — then seller, then platform fee, with per-leg rollback. Regression tests cover both completion paths. |
| Seller could replace the logistics beneficiary after funding | A provider committed at creation can only be confirmed; otherwise the first assignment is final. Replacement reverts, verified live on `SWFT-563283`. |
| Failed refunds / premature reputation | Refund callbacks restore the exact prior state; completion and refund reputation move only after their transfers succeed. |
| Zero/very-short inspection window | `create_escrow` enforces a **1-hour minimum inspection period** (UI defaults to 72h and the escrow page shows the auto-release time); the timeout release can never skip a real buyer review window. |
| Frozen funds if the board stalls | Buyer **dispute and long-stop refunds**: unresolved disputes default to a buyer refund after 7 days; undelivered shipments have a 30-day long-stop. No-dispatch refunds work even when the seller opened the dispute, verified live on `SWFT-563283`. |
| Upgrade key could redeploy the contract | Deploy keys were deleted on **both** `sweeft-pay.testnet` (tx `GPA8AfRemcEgnkVCL8CtwZJ6AcHaT18Q5yfeqwbdJkDg`) and `sweeft-board.testnet` (tx `BYxu9F3G…`); `view_access_key_list` returns **empty** for both — no key can deploy new code, ever. Owner/board actions remain signature-gated by their member accounts. |
| Carrier key churn | Carrier key/provider-id changes are **board-only** (owner cannot set them), and rotation is visible on-chain. |
| Pool reachable before a board exists | The review board is a **required init argument** (`new(..., arbiter, ...)`); a fresh deployment never exposes the pool, and the deploy script additionally requires the carrier key. |
| Login accepted scoped function-call keys | Sessions require a **FullAccess** on-chain key (verified against the access-key list), plus CSRF-protected POSTs, session fixation rotation, cache-backed rate limits, and a secret-key fail-hard when `DEBUG=0`. |
| AI budget cap lived only in server settings | Enforced **on-chain** in the budget contract (`get_max_payment`/`set_max_payment`, default 0.1 NEAR per payment). |

Residual (disclosed) risks:

- The review-board council is a **demo council** on testnet (single operator; multisig/hardware on mainnet).
- **Carrier key custody**: on testnet the Demo Carrier signing key is held by this project's service
  (`DEMO_CARRIER_PRIVATE_KEY` in the server env) — a real deployment would hold it at the carrier.
  The TEE verifies the signature, so trust is in the registered carrier key holder; the owner/board
  can rotate that key on-chain, and rotation is visible to verifiers.
- The demo agent key is server-side (on-chain cap + budget bound its blast radius).
- The arbitration pool has no slashing — which is why it is disabled in favour of the board.
- OutLayer compute refunds accumulate in the contract unspent: there is deliberately no owner sweep,
  so they are dust on testnet rather than a drain vector.
- **Signed carrier payloads carry no timestamp/nonce** (no replay window in this demo): production
  would add a signed `issued_at` + nonce with a replay cache in the verifier.
- The pending-invitation index is capped at 10 entries per account; filling a victim's list costs
  ~0.2 NEAR. A production deployment would charge a reclaimable bond instead.

## Tests

- Contract logic is exercised on testnet by `scripts/e2e_lifecycle.sh` (create → … → settle),
  asserting exact balances at each settlement step.
- Django: `python3 manage.py test` — carrier state-machine guards (forward-only, idempotent,
  failure-terminal), partner API auth + event round-trips, escrow tracking lookup, the
  verification payload matching the OutLayer invariants, and the relayer (chain-conditioned
  actions, inspection-window gating, failure isolation).
- Carrier simulator: `cd tools/demo-carrier && python3 -m unittest test_simulator -v`.
- Django smoke: `python3 manage.py check`; pages `/`, `/dashboard/`, `/escrow/<id>/`,
  `/logistics/`, `/partners/portal/`, plus the JSON API under `/api/v1/`.
- Rust unit tests: `cargo test` in `contracts/sweeft` — state machine, permissionless
  verification/settlement/release gates, beneficiary immutability, dispute deadlines, payout
  rollback and post-payout reputation (54 tests).

## Known limitations / roadmap

- Demo Carrier is simulated — a standalone service (`tools/demo-carrier/`) polling the partner API;
  real GIG/DHL/FedEx integrations require API access (not assumed).
- The verification module receives the carrier payload as input; fetching carrier APIs from inside
  the TEE is a WASI P2 upgrade path.
- `REVIEW_REQUIRED` has an explicit, disclosed demo-board path (`resolve_review`) plus a seven-day
  buyer-refund fallback — this is not decentralized arbitration.
- **Reputation economics are unproven**: counters are mechanism-complete and chain-derived, but on
  testnet wash-trading is cheap. Production would add stake, fees, or dispute-bond mechanics.
- Marketplace listings are an off-chain catalog (photos/descriptions); the on-chain reference is
  the wallet and its escrow history.
- Fiat/bank payouts, cross-chain settlement, reputation staking, and mobile apps are out of
  MVP scope.

License: MIT.
