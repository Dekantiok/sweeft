# Verified: two-wallet end-to-end (browser + on-chain)

Run on 2026-11-13 with two independent Meteor testnet wallets:

- **Seller / platform**: `steepdata3968.testnet`
- **Buyer**: `fewcap2636.testnet`

Both sessions were signed in through the app's NEP-413 login, verified against on-chain
access keys, and driven in a real browser (Chromium). On-chain steps were executed with the
wallets' own keys; the UI was asserted and screenshotted at every stage.

**Deployment history.** Sections 1–8 record runs against earlier testnet deployments of this
project (superseded accounts; escrow ids were scoped per deployment). The current deployment —
escrow `sweeft-escrow.testnet`, review board `sweeft-board.testnet`, both keyless — is
section 9, where escrow ids restart at `VP-000001`.

## 1. Native-NEAR lifecycle — `VP-000020` → `Completed`

| Step | Tx |
|---|---|
| `create_escrow` (seller) | `CWcDuVBD32foZjk5vKpHWwxWWgwQueRaKcAeNtryXUvK` |
| `accept_escrow` (buyer) | `4JF3eNSa5wXs7Z9NJ3tLLaFV3vLb14LrAtohF46qsUQe` |
| `fund_escrow` 0.115 NEAR (buyer) | `7maN4NBWT6CDgzTid6JeCiLrzTuJsZPu4qwJqZRG7qx4` |
| `assign_logistics_provider` (seller) | `7cjXuA8nxtuhYqBRY7ZveJjxKfcJ7wcmT6ek7KsdA5TH` |
| `register_shipment` (seller) | `8tFZ5EbiwNMnJhfEzbtEoxHcBfQYZLoAziD73ZTpYKXp` |
| OutLayer TEE verification (delivered) | callback recorded `verified: true` |
| `settle_logistics` | `8zDiAsz276RN4h7v5KAXQJCFrScmtXDZkLt2EvWbJ9uY` |
| `confirm_receipt` (buyer) | `Am4mhYJhBNHdPc3yFUtGH9cMepRH9tFFLVeLFKxknFMC` |

Final state `Completed`; seller, logistics and platform payout flags all true.

## 2. Dispute → review-board resolution — `VP-000021` → `Refunded`

- Buyer-funded escrow (0.115 NEAR); the **buyer tried to dispute while `Funded` and was
  correctly rejected on-chain** (`buyer cannot dispute in this state`) — the buyer dispute
  window opens at `Shipped`.
- Seller opened the dispute (`9FELVeLeAFUzDVG1X1wc5Qx6rVeMVUZgGuDbBXGyQUah`) →
  `ReviewRequired`.
- Board proposal **#5** (`resolve_review`, 60% to seller) approved by
  `seller.steepdata3968.testnet` + `logistics.steepdata3968.testnet` (the disputing
  seller's account recused itself) → DAO executed the split automatically.
- Result: **Refunded** — seller +0.069 NEAR, buyer +0.046 NEAR (60/40 of 0.115; the
  proposer's 0.1 NEAR bond also returned).

## 3. AI co-pilot payment from the native budget (browser)

- `steepdata3968` deposited into `budget.steepdata3968.testnet` (allowance 0.08 total).
- In the browser chat: *“pay 0.01 NEAR to fewcap2636.testnet for two-wallet E2E”* →
  `Pay now — co-pilot settles it` → **Paid ✓ tx `ANcYNSsa4N8S…`** (server-side agent
  signature, no wallet popup needed).
- On-chain deltas: buyer **+0.01 NEAR**, budget `spent` 0.015 → **0.025**, no other
  balance moved.

## 4. UI assertions (both sessions)

- Landing gating: unauthenticated sees one connect CTA; connected seller reveals the two
  paths via “Start trading”.
- Seller dashboard lists `VP-000020` (Completed) and `VP-000021` (Refunded); escrow page
  shows native amounts, acceptance policy, TEE verification record.
- Buyer dashboard lists both escrows as purchases; escrow page renders `Refunded` with the
  60/40 split; trust profile loads.
- Governance console shows the proposals and approvals; co-pilot chat renders the payment
  card and status with zero JS errors across all pages.

## 5. Post-review hardening verification (2026-11-13, contract v10)

Exploit paths from the pre-submission review, rejected live on the then-current deployment:

| Attempt | On-chain result |
|---|---|
| `vote_dispute` / `finalize_arbitration` with a review board set (seller-sockpuppet path) | reverted: `arbitration pool is disabled while a review board is set` |
| Owner retaking the board via `set_arbiter(owner)` | reverted: `board already set — only the current board can change it` |
| `withdraw(amount, to)` (owner drain) | `MethodNotFound` — method removed |
| `create_escrow` without a storage deposit | reverted: `attach at least 0.02 NEAR storage deposit` |
| `create_escrow` with an unlisted token | reverted: `token not allowed` |

Hardened end-to-end: **`VP-000022`** completed the full lifecycle through the new TEE verifier
(active hash `e5b1a7aa…`) with all payout flags; **`VP-000023`** exercised the tampered-provider
rejection path (contract-side identity checks + verifier unit tests; OutLayer executions were
intermittently timing out at the RPC layer during the run — the positive path above is the
authoritative TEE proof).

## 6. Second review round — fixes and verification (2026-11-13, contract v11, verifier v3)

- **Stranded logistics fix**: `resolve_review`/`finalize_arbitration` computed the split pot
  *after* flipping `logistics_paid`, subtracting the logistics share from the split and locking it
  in the contract. The pot is now computed from the pre-flip state, so an unpaid allocation is
  genuinely folded into the split. Regression tests assert `seller_part + buyer_part == total_amount`
  for both the board and pool paths (`dispute_split_with_unpaid_logistics_covers_the_full_total`,
  `arbitration_split_with_unpaid_logistics_covers_the_full_total`).
- **Carrier signatures (the real TEE fix)**: the Demo Carrier service signs the canonical payload
  (`v1|provider|tracking|shipment|status|events`, ed25519) and the TEE verifier requires a valid
  signature whenever a carrier pubkey is registered on-chain (`set_carrier_pubkey`).
  - `VP-000024`: signed payload → TEE `verified: true`, full lifecycle Completed.
  - `VP-000025`: tampered signature → TEE `verified: false — carrier signature invalid`,
    escrow stayed `Shipped` (no state advance).
- **Fresh-deploy safety**: `deploy_testnet.sh` now **requires `BOARD_ACCOUNT`** and calls
  `set_arbiter` (plus `set_provider_id`/`set_carrier_pubkey`) before finishing — a fresh
  deployment can no longer sit exploitable without a board.
- **Demo-council labelling**: README and the governance UI now state that the 2-of-3 council is a
  demo council operated by this project on testnet.
- OutLayer compute refunds accumulate unspent by design (no owner sweep exists); documented as dust.

## 7. Third review round — completion settles logistics, mandatory signatures (2026-11-13, contract v12)

- **Completion never strands logistics**: `confirm_receipt` and
  `release_after_inspection` now pay a still-due logistics allocation first
  (requires a recorded provider), then seller, then platform fee — per-leg
  rollback guards. Live proof on **`VP-000026`**: logistics was unpaid when the
  buyer confirmed; final `Completed` with flags `true/true/true`, provider
  **+0.01 NEAR**, treasury **+0.005 NEAR**.
- **Inspection floor**: `create_escrow` with a 1-second window reverts live
  (`inspection period below the 1 hour floor`).
- **Mandatory carrier signatures**: verifier v4 (`e584e8df…`) fails closed when
  no carrier key is configured on-chain; unsigned payloads never verify.
  The signed happy path above ran through v4 (verified true).
- **Init-time board**: `new(..., arbiter, provider_id, carrier_pubkey)` — a
  fresh deployment can no longer expose the pool or run unsigned; the deploy
  script requires `BOARD_ACCOUNT` and `CARRIER_PUBKEY`.

## Notes / follow-ups

- The buyer-dispute state gate is intentional (disputes open at `Shipped`); sellers can
  escalate earlier from `Funded`/`ShipmentPending` — this run exercised both.
- The public `rpc.testnet.near.org` endpoint now returns “deprecated” throttling; the app
  uses `https://test.rpc.fastnear.com` (set in `.env`).

## 8. Fourth review round — upgrade key, board-only carrier config, long-stop refunds (contract v13/v14)

- **Upgrade key deleted**: the then-current contract account had its single full-access key
  (`ed25519:51KXxJwbQuRLrZEdjsNCAokbgbeCKtcsJrixS5jbVuMS`) removed on-chain
  (tx `DJ7JTgygJ7mTLQZ7Fnow6VSj8SSfpxueJguGpcMdSqg9`); it ended with **no keys** —
  the contract code is immutable and the "no platform key" claim is literally true.
- **Carrier config is board-only**: `set_carrier_pubkey`, `set_provider_id` and
  `set_allowed_token` revert for the owner; only the review board can rotate them.
- **Long-stop refund**: 30 days after the dispatch deadline, an unverified shipment lets the
  buyer reclaim the unspent balance regardless of board activity (unit-tested before/after grace).
- **Verification requires a configured carrier key at request time** (contract guard), so no
  deployment can run unsigned checks even if the TEE verifier were older.
- Final lifecycle on this build: `VP-000027` / `VP-000028` — signed-carrier TEE verification,
  logistics settled, confirmation completed, all payout flags true
  (e.g. 027 settle `FnYKtT9GwjAL7SEXmF8Jc89SuZwFGk1LkaGRxmzMWU6K`).


## 9. Current deployment (2026-11-14, contract v14, verifier v4)

All repository addresses and links now point to this stack.

- **Contract `sweeft-escrow.testnet`**: faucet creation `dJ6VVswnDxXzXPaQdk2mU7MAc6z8nNLmePhrj7oe9LT`;
  wasm deploy `FfMqZAyN2JvMY5M48PPJBkFdvAEwuxwoneu4VJ8TjGVg`; `new(...)` init
  `7JAGPTqFGo64gC77WrC2RavECDyt53rfRphB4uYCnp5m` (owner `steepdata3968.testnet`, arbiter
  `sweeft-board.testnet`, provider `LOG-DEMO-001`, carrier key registered, fee 500 bps);
  treasury `set_treasury` `B8TfK5CrGc69bWLbZ7Wia5FZs8vJJMJfPf68Hy5Rn26t`.
- **Review board `sweeft-board.testnet`**: Sputnik DAO deploy
  `85coMCmPrBjj6CFCKbuoS8we57GXXY83bxDB7xYNVjvh`, init
  `4cugvvjT6MnMgqx3f3Wr9XPUPmzuauo6QDxqLRii4kk2` (council: `steepdata3968.testnet`,
  `seller.steepdata3968.testnet`, `logistics.steepdata3968.testnet`).
- **Keyless / immutable**: deploy keys deleted on-chain — escrow
  `GEibn3MXaMA6CNbuTE1GXfiHRjGSKjLcnYNPygPwcxcd`, board
  `BYxu9F3GcHhUxVKE8Qe7ufnRwEbVVJK8mZd5FKb9bTk6`; `view_access_key_list` returns `[]` for both.
- **TEE**: OutLayer project `steepdata3968.testnet/sweeft-verify` (wasm `e584e8df…`, verifier v4,
  mandatory carrier signatures).
- **Lifecycle `VP-000001`** (0.115 NEAR; seller `seller.steepdata3968.testnet`, buyer
  `steepdata3968.testnet`): create `QpG8oUFSKmgeTchrFtTtEjxLazfCYNL7JkwRJttYxR4` → accept
  `8uqtYpCrCxsA3xJNrETn6UwgEftMBTfVF3htWDSV3y8b` → fund
  `GiwanRoLz8dkZZaSJMAhCnjV3RdYp66bmpUEBx1SBDrd` → assign
  `Cbtc7MPfLiS83cXoTTeWdFvcHBumWsL5WsgY9vUdwTLd` → register
  `EDoF8cZ4KV75qn2KFxtFAwTHJi714wjPFBJxqZK3sRuS` → signed-carrier TEE verification requested
  `4uF5DjVkxp8itVkhvsJjzTCMNN1bHyqGRz9W2dzZVpmP` (verified on first poll) → settle logistics
  `HXV1pEqRJZSFGmorjxNB41mkugnh1MVVC6jTe5J7uSPk` → confirm receipt
  `D9BGKQfAJC4t5qNksqPf7nPgVqm8wrYYjA8TsV5icsMW` → `Completed`, flags `true/true/true`;
  payout deltas seller **+0.1**, logistics **+0.01**, platform fee **+0.005 NEAR** to the board
  treasury.
- **App check**: an authenticated NEP-413 session renders `/`, `/dashboard/`,
  `/escrow/VP-000001/` and `/governance/` with HTTP 200 and shows the current contract address
  (dashboard lists `VP-000001`, escrow page shows `Completed`, governance shows the board).

## 10. Pre-submission key rotation (2026-11-14)

- The development seed for `steepdata3968.testnet` was rotated as submission hygiene: a new
  full-access key `ed25519:CTGNf5iRs1Uovrq8E11zGT51DZZMVDqqWtEjt4MVPP2Q` was added
  (tx `8MorZ493EjqiQicMZFKFAEsbh2h8u86YaQbSDhbmNrj9`), proven with a signed 1-yocto
  self-transfer (`HLpj65wfNrvf3hCAC8EMo9ih1ckFjgKNbmnYpigU2KUp`), and the old key
  `ed25519:ELxotH9SWYHkGBJPkdybXY1ocdP9wVoDUipjnFZs8E2z` was deleted on-chain
  (tx `BeJe8EiY5V9mA7bf5EP5kyp5NSLUpAZ5WVSLdvaVo9F4`). Credentials live at
  `~/.near-credentials/testnet/steepdata3968.testnet.json` (mode 0600).
- Reproducible via `scripts/rotate_key.py <account>`; the account's scoped FunctionCall key
  (0.25 NEAR allowance) is unaffected.
