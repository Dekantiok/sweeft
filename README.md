# VeriPay

**Wallet-based escrow, logistics verification, settlement — and a reputation marketplace — for physical commerce on NEAR testnet.**

Buyers do not send money to an unknown seller before goods ship. Sellers know the buyer funded the
deal. Logistics gets paid only when carrier pickup is verified, the seller only after TEE-verified
delivery plus explicit buyer confirmation, and the platform fee is disclosed before funding.

One wallet connects the whole ecosystem: **marketplace listings → on-chain trust profile → escrow →
verified logistics → settlement → reputation**. Every reputation number is contract-derived and
recomputable by anyone — the marketplace is discovery, the chain is the truth.

```
CONNECT WALLET → BROWSE / LIST → CHECK TRUST PROFILE → ESCROW
→ ACCEPT → FUND (native NEAR) → SHIP → TEE-VERIFY DELIVERY → CONFIRM RECEIPT
→ SETTLE (seller + logistics + platform) → REPUTATION MOVES
```

Built at NEAR HACKATHON (Nov 2026). Baseline: new repository created 2026-11-09.

---

## What is real vs simulated

| Component | Status |
|---|---|
| Escrow + authorization + settlement | **Real** — Rust contract on NEAR testnet (`veripay.steepdata3968.testnet`) |
| Funding currency | **Native NEAR** — payable `fund_escrow`; no token mint, import, or registration. Legacy NEP-141 tUSD escrows remain supported |
| Wallet identity + authorization | **Real** — NEP-413 signature + on-chain access-key ownership verified server-side; every financial action is wallet-signed |
| Delivery verification | **Real TEE** — OutLayer (Intel TDX) executes our published WASM verifier; result returns on-chain via the contract callback |
| Carrier tracking data | **SIMULATED** — Demo Carrier; labelled `SIMULATED DATA` in the DB (`simulated=True`), API responses and UI |
| Platform fee | **Real** — 5% of seller amount, enforced by the contract, paid at completion |
| AI co-pilot payments | **Real on-chain** — scoped, revocable native-NEAR budget (`budget.steepdata3968.testnet`); the agent pays only within the user's deposited limit |
| Dispute arbitration pool | **Real** — any NEAR holder stakes NEAR to vote the split; quorum → permissionless settlement; voters reclaim stake |
| Review board (Sputnik DAO) | **Real** — disputes resolve ONLY via council-approved on-chain proposals (2-of-3); the platform owner has no resolution key |
| Treasury (DAO multisig) | **Real** — platform fees accrue to the board account; spending requires a council-approved Transfer proposal |
| Fiat payouts / real carriers | **Not implemented** (out of MVP scope) |

Boundaries we state plainly: a carrier scan does not prove parcel contents; buyer confirmation is a
separate settlement condition; a silent buyer never auto-releases funds.

## Verified end-to-end on testnet

### Native NEAR (current default)

- **`VP-000014`** — full lifecycle in native NEAR: buyer funded **0.115 NEAR** with the payable
  `fund_escrow`; OutLayer TEE verified delivery; seller received **0.1 NEAR**, logistics **0.01 NEAR**,
  and the review-board treasury received the **0.005 NEAR** platform fee.
- **`VP-000015`** — native dispute: review-board proposal #3 approved 2-of-3 and executed
  `resolve_review(…, 60)` → seller **0.0414 NEAR**, buyer **0.0276 NEAR** (before gas).
- **AI budget** — owner deposited **0.05 NEAR**; the co-pilot paid invoices in native NEAR and the
  budget's `spent`/`available` updated exactly on-chain. No token mint or registration anywhere.

### Legacy FT verification (historical tUSD escrows)

Full lifecycle proven with real transactions and exact balances (see `scripts/e2e_lifecycle.sh`):

- `VP-000003`: seller `seller.steepdata3968.testnet`, buyer `steepdata3968.testnet`,
  provider `logistics.steepdata3968.testnet`
  - funded 115 tUSD via `ft_transfer_call`
  - OutLayer TEE executed the published verifier (`3153c52b…`, active version of the
    `steepdata3968.testnet/veripay-verify` project) and returned `verified: true` for the
    registered tracking number; the contract recorded `pickup_verified` + `delivered_verified`
  - `settle_logistics` paid the provider **+10 tUSD**
  - `confirm_receipt` paid the seller **+100 tUSD** and the platform **+5 tUSD**
  - contract balance returned to **0**; state `Completed`

- `VP-000004` (reputation cycle): full lifecycle; afterwards `reputation_of(seller)` shows
  `completed_as_seller: 1`, `reputation_of(buyer)` shows `completed_as_buyer: 1`, both with
  `volume: 100000000`; `VP-000005` reject moved `requests_rejected: 1` for the declining buyer.
- Marketplace verified in-app: 4 seeded listings browse at `/market/`, seller trust panel reads
  live contract counters, profiles at `/u/<wallet>/`, purchase requests → prefilled escrow.

TEE negative case also verified: a tampered tracking number returns
`verified: false` with the mismatch reason — natively (unit tests) and inside the real TEE.

### Governance verified on testnet (Sputnik DAO review board)

Board: [`veripay-board.testnet`](https://testnet.near.org/accounts/veripay-board.testnet) — council 2-of-3
(`steepdata3968` + `seller.steepdata3968` + `logistics.steepdata3968`), default Sputnik policy.

- **Proposal #0** — council voted to lower the proposal bond to 0.1 NEAR (governance parameter change, executed).
- **Proposal #1** — dispute on `VP-000012`: the seller (a party) filed the escalation; the **other two council
  members approved** (the proposing party recused itself) → the DAO executed `resolve_review(VP-000012, 60)`
  on-chain → exact split: seller **+69 tUSD**, buyer **+46 tUSD** of the 115 tUSD escrow.
- **Proposal #2** — treasury spend: 0.2 NEAR from the board account to the logistics partner
  (arbitration participation incentive) — executed via Transfer proposal.
- **Platform fees → treasury**: completing `VP-000006` paid the 5 tUSD fee to the board account. (First attempt
  failed because the treasury wasn't registered in the token — the payout rolled back safely and the new
  permissionless `retry_platform_payout` recovered the funds on-chain: treasury **+5 tUSD**.)

The escrow contract's `get_gov_info` shows `arbiter`/`treasury` = the DAO; the platform owner can appoint the board
but **cannot resolve disputes** — that power is gone by design.

Explorer: <https://testnet.near.org/accounts/veripay.steepdata3968.testnet>

## Architecture

```
contracts/veripay/     Rust (near-sdk) escrow contract — funds & authorization source of truth
contracts/test-token/  Legacy NEP-141 tUSD (only for pre-native escrows)
contracts/near-budget/ Native-NEAR co-pilot budget (deposit / agent pay / withdraw)
wasi/veripay-verify/   TEE verification program → wasm32-wasip2, published on OutLayer
veripay/               Django modular monolith (product coordinator + projection)
  apps/wallets/        NEP-413 challenge/verify, sessions
  apps/escrow/         agreements, invitations, contract reads, settlement projection
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

Escrows, marketplace prices, logistics fees, platform fees, arbitration stakes, and co-pilot
budgets all use **native NEAR**. There is no project token to mint, import, or register.

- **Already have testnet NEAR?** Connect your wallet and go — the header chip shows your native balance.
- **Fresh wallet?** Click **“Get test NEAR”** in the header or dashboard to open the public
  [NEAR testnet faucet](https://near-faucet.io/). Any testnet wallet works.

Legacy escrows created before the native migration still display and settle through their original
NEP-141 token (`tusd.steepdata3968.testnet`); new escrows pass `token: "near"` and fund with the
payable `fund_escrow` method.

## Setup

```bash
# 1. Django app (Python 3.10+, Django 5)
cd veripay
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
| `VERIPAY_CONTRACT` | deployed escrow account |
| `AI_BUDGET_CONTRACT` | native-NEAR co-pilot budget account |
| `TUSD_CONTRACT` | legacy NEP-141 token account (old escrows only) |
| `OUTLAYER_CONTRACT` / `OUTLAYER_PROJECT` | OutLayer receiver + published verification project |
| `NEAR_NETWORK`, `NEAR_RPC_URL`, `DJANGO_DEBUG`, `DJANGO_SECRET_KEY` | standard config |

### Contracts

```bash
rustup target add wasm32-unknown-unknown wasm32-wasip2
cargo install cargo-near          # or grab the prebuilt release

(cd contracts/veripay && cargo near build non-reproducible-wasm --no-abi)
(cd contracts/near-budget && cargo near build non-reproducible-wasm --no-abi)
# legacy token only if you need it: (cd contracts/test-token && cargo near build non-reproducible-wasm --no-abi)

# deploy + init (see scripts/deploy_testnet.sh for the full flow)
near contract deploy <acct> use-file target/near/veripay.wasm without-init-call ...
near contract call-function as-transaction <acct> new json-args '{...}' ...
```

### Publish the TEE verifier (OutLayer)

```bash
curl -fsSL https://raw.githubusercontent.com/out-layer/outlayer-cli/main/install.sh | sh
# (or: cargo install outlayer-cli)
outlayer login testnet
(cd wasi/veripay-verify && cargo build --target wasm32-wasip2 --release)
outlayer upload wasi/veripay-verify/target/wasm32-wasip2/release/veripay-verify.wasm
outlayer deploy veripay-verify <FastFS URL printed by upload>
```

### Frontend bundle (only if you change wallet code)

```bash
cd tools/web-bundle && npm install && npm run build   # writes veripay/static/js/veripay-wallet.js
```

## Demo workflow

1. Connect a wallet (Meteor / HERE / Nightly — sign the free NEP-413 message to sign in).
2. **Marketplace**: browse `/market/`, open a listing, inspect the seller's **trust profile**
   (`/u/<wallet>/` — live contract reputation), then "Request to buy via escrow".
3. The seller sees the purchase request under `/market/mine/` and creates the escrow — prefilled
   from the listing — with a wallet-signed `create_escrow`.
4. Buyer accepts, then **funds** the exact total in native NEAR. Logistics is assigned and the Demo
   Carrier tracking number is registered.
5. **Dispatch starts the carrier run**: at "Mark dispatched" pick a scenario (`delivered` in ~60s,
   or `failed` / `lost` for dispute demos). The standalone Demo Carrier simulator plays
   receive → transport → deliver over the partner API; the escrow page shows the live timeline.
6. Any party requests **TEE delivery verification** — OutLayer runs the published verifier and the
   contract callback records the attested result.
7. Seller/provider **settle logistics** (verified pickup); buyer **confirms receipt** → seller +
   platform payouts settle, the listing flips to `sold`, and **reputation moves on-chain for both
   wallets** — visible instantly on their profiles. Platform fees land in the **review-board treasury**.
8. **Governance**: while an escrow is `ReviewRequired`, either party can **Escalate to the review board**
   from the escrow page — a wallet-signed DAO proposal (`resolve_review`); council members approve on
   `/governance/`; at 2-of-3 the DAO executes the split on-chain. The arbitration pool stays as the
   permissionless community tier, the board is the governed tier.

Scripted version of the escrow path: `scripts/e2e_lifecycle.sh`.

### Demo Carrier simulator (SIMULATED — separate process)

The carrier is a **standalone service**, not part of Django: it talks only to the key-authenticated
partner API (the same surface a real GIG/DHL/FedEx integration would use) and holds no database
state. Start it next to `runserver`:

```bash
python3 manage.py seed_demo --issue-carrier-key          # one-time: prints the vp_… key
VERIPAY_BASE_URL=http://127.0.0.1:8000 DEMO_CARRIER_API_KEY=vp_… \
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
  treasury balances (NEAR + tUSD), treasury Transfer proposals, and the on-chain wiring
  (`arbiter`/`treasury`).
- Escrow page — “Review board” panel appears while a dispute is open: escalation form for the parties,
  Approve/Reject for council members, live vote chips.
- Contract — `get_gov_info`, `set_arbiter`, `set_treasury` (+ 7 Rust unit tests for the authorization
  rules) and permissionless `retry_platform_payout` for failed fee transfers.

## Tests

- Contract logic is exercised on testnet by `scripts/e2e_lifecycle.sh` (create → … → settle),
  asserting exact token balances at each settlement step.
- Django: `python3 manage.py test` — carrier state-machine guards (forward-only, idempotent,
  failure-terminal), partner API auth + event round-trips, escrow tracking lookup, and the
  verification payload matching the OutLayer invariants.
- Carrier simulator: `cd tools/demo-carrier && python3 -m unittest test_simulator -v`.
- Django smoke: `python3 manage.py check`; pages `/`, `/dashboard/`, `/escrow/<id>/`,
  `/logistics/`, `/partners/portal/`, plus the JSON API under `/api/v1/`.
- Rust unit tests: `cargo test` in `contracts/veripay` (state-machine helpers).

## Known limitations / roadmap

- Demo Carrier is simulated — a standalone service (`tools/demo-carrier/`) polling the partner API;
  real GIG/DHL/FedEx integrations require API access (not assumed).
- The verification module receives the carrier payload as input; fetching carrier APIs from inside
  the TEE is a WASI P2 upgrade path.
- `REVIEW_REQUIRED` has an explicit, disclosed operator path (`resolve_review`) — this is a
  centralized fallback, not arbitration.
- **Reputation economics are unproven**: counters are mechanism-complete and chain-derived, but on
  testnet wash-trading is cheap. Production would add stake, fees, or dispute-bond mechanics.
- Marketplace listings are an off-chain catalog (photos/descriptions); the on-chain reference is
  the wallet and its escrow history.
- Fiat/bank payouts, cross-chain settlement, reputation staking, and mobile apps are out of
  MVP scope.

License: MIT.
