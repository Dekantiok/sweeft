# Development setup

## Toolchain

| Tool | Version used |
|---|---|
| Python / Django | 3.10 / Django 5.2 |
| Rust (contracts) | stable + `wasm32-unknown-unknown`; `cargo-near` |
| Rust (TEE module) | stable + `wasm32-wasip2` |
| Node (bundle only) | 20.x + npm (esbuild) |
| near-cli-rs | 0.31.x |
| outlayer CLI | 0.2.x (`cargo install outlayer-cli`) |

## Run the web app

```bash
cd veripay
cp .env.example .env          # pre-filled with the deployed testnet addresses
python3 -m pip install django django-environ
python3 manage.py migrate
python3 manage.py createsuperuser    # partner approvals live in /admin/
python3 manage.py runserver 0.0.0.0:8000
```

### Run the Demo Carrier simulator (second terminal)

The Demo Carrier is a standalone service (`tools/demo-carrier/`) that plays a ~60-second
delivery lifecycle over the carrier Partner API, starting once an escrow is dispatched on-chain.
It needs a partner API key (printed once):

```bash
python3 manage.py seed_demo --issue-carrier-key
VERIPAY_BASE_URL=http://127.0.0.1:8000 DEMO_CARRIER_API_KEY=vp_… \
  python3 tools/demo-carrier/simulator.py
```

Without it, shipments simply wait at `CREATED` — the escrow page shows “waiting for carrier”.
Its unit tests run standalone: `cd tools/demo-carrier && python3 -m unittest test_simulator -v`.

### Run the verification relayer (third terminal)

The platform relayer drives the permissionless happy path on-chain (TEE verification → logistics
payout → inspection-window release). Set a dedicated funded testnet account in `.env`
(`RELAYER_ENABLED=1`, `RELAYER_ACCOUNT`, `RELAYER_PRIVATE_KEY`) and run:

```bash
python3 manage.py run_relayer        # loop; --once for a single pass / cron
```

Without it, the escrow page keeps manual fallback buttons (wallet-signed) — nothing else breaks.

Pages: `/` (landing), `/dashboard/`, `/escrow/new/`, `/escrow/<id>/`, `/invitations/<token>/`,
`/logistics/`, `/partners/register/`, `/partners/portal/`, `/admin/`.

## Environment variables

See `veripay/.env.example`. Never commit real values; the repo contains none.

## Rebuild artifacts

```bash
# contracts
(cd contracts/test-token && cargo near build non-reproducible-wasm --no-abi)
(cd contracts/veripay   && cargo near build non-reproducible-wasm --no-abi)

# TEE verification module (WASI Preview 2 — required by the OutLayer worker)
(cd wasi/veripay-verify && cargo build --target wasm32-wasip2 --release)

# wallet bundle (static JS used by the Django templates)
(cd tools/web-bundle && npm install && npm run build)
```

## Tests

```bash
python3 manage.py check                                # Django system check
python3 manage.py test                                # Django unit tests (logistics + relayer + API)
(cd tools/demo-carrier && python3 -m unittest test_simulator -v)   # carrier simulator schedule
(cd contracts/veripay && cargo test)                   # contract unit tests (22: state machine + permissionless gates)
SELLER_KEY=... BUYER_KEY=... scripts/e2e_lifecycle.sh  # full testnet lifecycle
```

The e2e script prints balances before/after each settlement step; expected deltas:
logistics +`LOGISTICS_AMOUNT`, seller +`SELLER_AMOUNT`, platform +5%.

## Common pitfalls

- **OutLayer modules must be `wasm32-wasip2`** — the worker rejects `wasm32-wasip1` ("Not a valid
  WASI Preview 2 component"). Contracts must be `wasm32-unknown-unknown`.
- **NEP-141 receivers must be storage-registered** on the token contract before a transfer reaches
  them (`storage_deposit`); a failed transfer rolls the payout flag back so it can be retried.
- **Wallet actions need NAJ-format actions** (`transactions.functionCall(...)`) — raw
  `{type, params}` objects throw "Unsupported NAJ action" in wallet-selector ≥10.
- RPC view calls use `request_type: "call_function"` (not `"call"`).
