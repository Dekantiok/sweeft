# VeriPay

**Wallet-based escrow, logistics verification and settlement for physical commerce — NEAR testnet MVP.**

Buyers do not send money to an unknown seller before goods ship. Sellers know the buyer funded the
deal. Logistics gets paid only when carrier pickup is verified, the seller only after TEE-verified
delivery plus explicit buyer confirmation, and the platform fee is disclosed before funding.

```
CONNECT WALLET → CREATE AGREEMENT → ACCEPT → FUND (tUSD) → SHIP
→ TEE-VERIFY DELIVERY → CONFIRM RECEIPT → SETTLE (seller + logistics + platform)
```

Built at NEAR HACKATHON (Nov 2026). Baseline: new repository created 2026-11-09.

---

## What is real vs simulated

| Component | Status |
|---|---|
| Escrow + authorization + settlement | **Real** — Rust contract on NEAR testnet (`veripay.steepdata3968.testnet`) |
| Funding token | **Real NEP-141** — `tusd.steepdata3968.testnet` (open-mint **testnet demo token**, not a stablecoin) |
| Wallet identity + authorization | **Real** — NEP-413 signed challenges verified server-side; every financial action is wallet-signed |
| Delivery verification | **Real TEE** — OutLayer (Intel TDX) executes our published WASM verifier; result returns on-chain via the contract callback |
| Carrier tracking data | **SIMULATED** — Demo Carrier; labelled `SIMULATED DATA` in the DB (`simulated=True`), API responses and UI |
| Platform fee | **Real** — 5% of seller amount, enforced by the contract, paid at completion |
| Fiat payouts / real carriers / arbitration | **Not implemented** (out of MVP scope) |

Boundaries we state plainly: a carrier scan does not prove parcel contents; buyer confirmation is a
separate settlement condition; a silent buyer never auto-releases funds.

## Verified end-to-end on testnet

Full lifecycle proven with real transactions and exact balances (see `scripts/e2e_lifecycle.sh`):

- `VP-000003`: seller `seller.steepdata3968.testnet`, buyer `steepdata3968.testnet`,
  provider `logistics.steepdata3968.testnet`
  - funded 115 tUSD via `ft_transfer_call`
  - OutLayer TEE executed the published verifier (`84ed000b…` → `7393b6ff…`) and returned
    `verified: true` for the registered tracking number; the contract recorded
    `pickup_verified` + `delivered_verified`
  - `settle_logistics` paid the provider **+10 tUSD**
  - `confirm_receipt` paid the seller **+100 tUSD** and the platform **+5 tUSD**
  - contract balance returned to **0**; state `Completed`

Explorer: <https://testnet.near.org/accounts/veripay.steepdata3968.testnet>

## Architecture

```
contracts/veripay/     Rust (near-sdk) escrow contract — funds & authorization source of truth
contracts/test-token/  Minimal NEP-141 tUSD (open mint, testnet demo token)
wasi/veripay-verify/   TEE verification program → wasm32-wasip2, published on OutLayer
veripay/               Django modular monolith (product coordinator + projection)
  apps/wallets/        NEP-413 challenge/verify, sessions
  apps/escrow/         agreements, invitations, contract reads, settlement projection
  apps/logistics/      partners, Demo Carrier, shipments, verification records
  templates/ static/   Django templates + HTMX + Alpine + Tailwind; wallet bundle
tools/web-bundle/      esbuild bundle of @near-wallet-selector (Meteor/HERE/Nightly)
scripts/               deploy + end-to-end scripts
docs/                  architecture, lifecycle, settlement rules, verification, partner API
```

**Source-of-truth rule:** the contract controls funds and permissions; Django coordinates the
experience and keeps a searchable projection; carrier systems report events; OutLayer attests to the
execution of the configured verification code; the buyer's confirmation remains a separate condition.

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
| `TUSD_CONTRACT` | NEP-141 token account |
| `OUTLAYER_CONTRACT` / `OUTLAYER_PROJECT` | OutLayer receiver + published verification project |
| `NEAR_NETWORK`, `NEAR_RPC_URL`, `DJANGO_DEBUG`, `DJANGO_SECRET_KEY` | standard config |

### Contracts

```bash
rustup target add wasm32-unknown-unknown wasm32-wasip2
cargo install cargo-near          # or grab the prebuilt release

(cd contracts/test-token && cargo near build non-reproducible-wasm --no-abi)
(cd contracts/veripay && cargo near build non-reproducible-wasm --no-abi)

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
2. **New agreement**: product, seller amount, logistics fee, dispatch deadline, inspection window,
   buyer wallet (or leave empty for a shareable invitation link). Create → wallet-signed `create_escrow`.
3. Buyer opens the invitation (or dashboard), **accepts**, then **funds** the exact total in tUSD.
4. Seller assigns the approved logistics provider, then registers the Demo Carrier tracking number.
   (Demo scans are advanced from the partner portal — clearly labelled simulated.)
5. Any party requests **TEE delivery verification** — OutLayer runs the published verifier and the
   contract callback records the attested result.
6. Seller/provider **settle logistics** (after verified pickup); buyer **confirms receipt** →
   seller + platform payouts settle; the escrow shows `Completed` with explorer links.

Scripted version of the same path: `scripts/e2e_lifecycle.sh`.

## Tests

- Contract logic is exercised on testnet by `scripts/e2e_lifecycle.sh` (create → … → settle),
  asserting exact token balances at each settlement step.
- Django smoke: `python3 manage.py check`; pages `/`, `/dashboard/`, `/escrow/<id>/`,
  `/logistics/`, `/partners/portal/`, plus the JSON API under `/api/v1/`.
- Rust unit tests: `cargo test` in `contracts/veripay` (state-machine helpers).

## Known limitations / roadmap

- Demo Carrier is simulated (real GIG/DHL/FedEx integrations require API access — not assumed).
- The verification module receives the carrier payload as input; fetching carrier APIs from inside
  the TEE is a WASI P2 upgrade path.
- `REVIEW_REQUIRED` has an explicit, disclosed operator path (`resolve_review`) — this is a
  centralized fallback, not arbitration.
- Fiat/bank payouts, Intents cross-chain settlement, reputation and mobile apps are out of MVP scope.

License: MIT.
