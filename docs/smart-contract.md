# Smart contract

Rust, `near-sdk` 5.x, deployed at **`veripay.steepdata3968.testnet`**.
The contract is the financial and authorization source of truth.

## Methods

### Lifecycle
| Method | Who | Effect |
|---|---|---|
| `new(owner, outlayer_contract, outlayer_project, platform_fee_bps)` | init | configure |
| `create_escrow(product_name, product_desc_hash, seller_amount, logistics_amount, buyer?, invite_hash?, token, dispatch_deadline_ms, inspection_period_ms, terms_hash)` | anyone (becomes seller) | new escrow, fee enforced, seller indexed, buyer pending |
| `accept_escrow(escrow_id, invite_secret?)` | buyer / invitee | `Approved` |
| `reject_escrow(escrow_id)` | buyer | `Rejected` |
| `cancel_escrow(escrow_id)` | seller | `Cancelled` (pre-acceptance) |
| `ft_on_transfer(sender_id, amount, msg)` | token contract | validates & records funding (`msg = escrow_id`) |

### Logistics & verification
| Method | Who | Effect |
|---|---|---|
| `assign_logistics_provider(escrow_id, provider)` | seller | records beneficiary |
| `register_shipment(escrow_id, tracking_number)` | seller/provider | `Shipped` (unlocks nothing by itself) |
| `request_delivery_verification(escrow_id, carrier_payload)` — payable | any party | calls OutLayer, results via callback |
| `on_verification_result(escrow_id)` — `#[private]` | contract itself | records TEE outcome |

### Settlement
| Method | Who | Effect |
|---|---|---|
| `settle_logistics(escrow_id)` | seller/provider | pays logistics after verified pickup |
| `confirm_receipt(escrow_id)` | buyer | pays seller, then platform fee |
| `report_problem(escrow_id, reason_hash)` | buyer | `ReviewRequired`, funds locked |
| `resolve_review(escrow_id, seller_pct)` | review board (DAO) only | splits remaining balance; owner has no resolution key |
| `refund_buyer(escrow_id)` | buyer | after dispatch deadline, no shipment |
| `withdraw(amount, to)` | owner | NEAR-only sweep (verification deposits / compute refunds) |
| `retry_platform_payout(escrow_id)` | anyone | re-attempt a failed platform-fee payout |
| `update_platform_fee_bps(bps)` | owner | future agreements only |

> There is deliberately **no `withdraw_ft`**: a fungible-token sweep would let the
> owner drain balances held for active escrows. FT leaves the contract only through
> escrow settlement, arbitration, or board resolution.

### Views
`get_escrow(id)`, `escrows_for(account)`, `pending_for(account)`, `get_config()`.

## Security properties

- Every state transition validated; permissions enforced on-chain; beneficiary addresses immutable
  after funding.
- Verification flags are written **only** by the `#[private]` OutLayer callback.
- Idempotency: `*_paid` flags + state checks prevent double settlement; failed FT transfers roll
  their flags back (`on_payout_result`) so a payout is never falsely reported.
- No keys or secrets in the contract; no backend endpoint can release funds.
- Duplicate funding rejected by state (`Approved` → `Funded` once); wrong token / amount / buyer
  refunded by `ft_on_transfer`.

## Build & deploy

```bash
rustup target add wasm32-unknown-unknown
cargo install cargo-near   # or prebuilt release

(cd contracts/veripay && cargo near build non-reproducible-wasm --no-abi)
near contract deploy veripay.<parent>.testnet use-file contracts/veripay/target/near/veripay.wasm \
  without-init-call network-config testnet sign-with-legacy-keychain send
near contract call-function as-transaction veripay.<parent>.testnet new \
  json-args '{"owner":"<parent>.testnet","outlayer_contract":"outlayer.testnet","outlayer_project":"<parent>.testnet/veripay-verify","platform_fee_bps":500}' \
  prepaid-gas '100 Tgas' attached-deposit '0 NEAR' sign-as veripay.<parent>.testnet network-config testnet sign-with-legacy-keychain send
```

`scripts/deploy_testnet.sh` performs the whole flow including the token and OutLayer publish step.
