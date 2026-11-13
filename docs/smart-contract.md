# Smart contract

Rust, `near-sdk` 5.x, deployed at **`sweeft.testnet`**.
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
| `fund_escrow(escrow_id)` — payable | buyer | native-NEAR funding; attached deposit must equal `total_amount` |
| `ft_on_transfer(sender_id, amount, msg)` | token contract | NEP-141 funding (`msg = escrow_id`) |

**Currency model:** new escrows use `token: "near"` and amounts in yoctoNEAR. A single `payout`
helper transfers native NEAR for `token: "near"` escrows and calls `ft_transfer` for NEP-141 escrows.

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
| `settle_logistics(escrow_id)` | permissionless | pays logistics after verified pickup; state-gated (never in review/settled states) |
| `confirm_receipt(escrow_id)` | buyer | pays seller, then platform fee (exactly once) |
| `release_after_inspection(escrow_id)` | permissionless | pays seller + platform after the inspection window elapses with no problem reported |
| `report_problem(escrow_id, reason_hash)` / `open_dispute(...)` | buyer/seller | `ReviewRequired`, funds locked |
| `resolve_review(escrow_id, seller_pct)` | review board (DAO) only | splits remaining balance; marks folded logistics paid; stores the buyer remainder |
| `retry_review_buyer_payout(escrow_id)` | permissionless | re-attempts only the stored buyer remainder after a failed leg |
| `refund_buyer(escrow_id)` | buyer | after dispatch deadline, no shipment (exactly-once reputation) |
| `retry_platform_payout(escrow_id)` | permissionless | re-attempts a failed platform-fee payout |
| `update_platform_fee_bps(bps)` | owner or board | future agreements only |

> There is deliberately **no `withdraw` and no `withdraw_ft`**: any owner sweep could
> drain balances held for active escrows. Value leaves the contract only through
> settlement or review-board resolution.

### Governance / configuration
| Method | Who | Effect |
|---|---|---|
| `set_arbiter(account)` | owner (bootstrap only) / current board | review board; **disables the arbitration pool** |
| `set_treasury(account)` | owner (bootstrap only) / treasury or board | platform-fee destination |
| `set_provider_id(id)` | review board | carrier-registry id enforced in verification outcomes |
| `set_allowed_token(token?)` | review board | FT allowlist (native NEAR always allowed) |
| `vote_dispute` / `finalize_arbitration` | — | **revert while a review board is set** |
| `claim_stake(escrow_id)` | voters | reclaim stake once a case is finalized (board resolution finalizes open cases) |

### Views
`get_escrow(id)`, `escrows_for(account)`, `pending_for(account)`, `get_config()`,
`get_gov_info()`, `get_provider_id()`, `get_allowed_token()`, `arbitration_status(id)`,
`delivery_time_ms(id)`, `reputation_of(account)`, `escrows_range(start, limit)`.

## Security properties

- Every state transition validated; permissions enforced on-chain; beneficiary addresses immutable
  after funding.
- Verification flags are written **only** by the `#[private]` OutLayer callback.
- Idempotency: per-leg flags + state checks prevent double settlement; failed native or FT
  transfers roll their flags back (`on_payout_result`) so a payout is never falsely reported, and
  a failed buyer leg never un-marks a paid seller leg.
- Creation requires a storage deposit and bounded strings; per-account indexes and pending lists
  are capped; the token field is an allowlist. Reputation counters are exactly-once and
  value-filtered (anti-wash).
- No keys or secrets in the contract; no backend endpoint can release funds; no owner withdraw.
- Duplicate funding rejected by state (`Approved` → `Funded` once); wrong token / amount / buyer
  refunded by `ft_on_transfer`.
- Honest TEE scope: the callback records the attested outcome, the contract enforces tracking and
  carrier-provider identity, and the verifier enforces shipment identity + timeline consistency;
  carrier authenticity itself needs signed carrier reports (roadmap).

## Build & deploy

```bash
rustup target add wasm32-unknown-unknown
cargo install cargo-near   # or prebuilt release

(cd contracts/sweeft && cargo near build non-reproducible-wasm --no-abi)
near contract deploy sweeft.<parent>.testnet use-file contracts/sweeft/target/near/sweeft.wasm \
  without-init-call network-config testnet sign-with-legacy-keychain send
near contract call-function as-transaction sweeft.<parent>.testnet new \
  json-args '{"owner":"<parent>.testnet","outlayer_contract":"outlayer.testnet","outlayer_project":"<parent>.testnet/sweeft-verify","platform_fee_bps":500}' \
  prepaid-gas '100 Tgas' attached-deposit '0 NEAR' sign-as sweeft.<parent>.testnet network-config testnet sign-with-legacy-keychain send
```

`scripts/deploy_testnet.sh` performs the whole flow including the token and OutLayer publish step.
