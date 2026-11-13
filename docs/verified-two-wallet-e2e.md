# Verified: end-to-end on NEAR testnet

Current deployment: escrow **`sweeft-pay.testnet`**, review board **`sweeft-board.testnet`** (both
keyless/immutable — deploy keys deleted), TEE project **`steepdata3968.testnet/sweeft-verify`**,
carrier signing key registered on-chain.

## Lifecycle — `SWFT-374253` → `Completed`

| Step | Tx |
|---|---|
| `create_escrow` (seller) | `76mtZFGTKUGxiASXdmhMA2P7wPXpSJCTcKCb6gd4D1vr` |
| `accept_escrow` (buyer) | `CvJr2CmyUqaBfYFpPG6UVMnYs4kSMcB7J5zxok8pfGaE` |
| `fund_escrow` 0.115 NEAR | `GoB9D3NRAih5JNYs3MiaTGJx98Z8cptYHiGjZhd3TfsU` |
| `assign_logistics_provider` | `EsJfnrqb1TjKzHQ57jmpKZ2Ls4k48GoyPG7Vpvfa9mP5` |
| `register_shipment` | `5mK2AtGdaKgBKvzeYvLAXe3NkJLWv5RcxaDgp4RwZrRF` |
| TEE verification (signed carrier payload) | `6WyNXvRGtwq9Eea9LawZms8YTrLKrX1AJM4oaKXuKmb4` |
| `settle_logistics` | `J6i6b4Gx6EcxdkSXMWEgneZBtC5fXn3fbNnhane3o7LJ` |
| `confirm_receipt` | `6ohfAex6Pid6k1z55pQFj3bGhUhmHBFGM8FscTiG4wZs` |

Final state `Completed`; seller, logistics and platform payout flags all true. Payout deltas:
seller **+0.1 NEAR**, logistics **+0.01 NEAR**, platform fee **+0.005 NEAR** to the board treasury.
The signed carrier result verified on the first poll. Post-settlement reputation shows exactly one
completion and 0.1 NEAR of volume for each party.

## Live hardening probe — `SWFT-563283` → `Refunded`

| Step | Result / tx |
|---|---|
| Create with logistics beneficiary committed | `E37nHYxKBuYLbs35rZ7JCyUv5s9Loo6NLALh8PnBTiqa` |
| Accept | `2a7WCmx3NdMdqancwXsxNFKpSrZierVn3izC8w6hPART` |
| Fund 0.115 NEAR | `5887svuPegUAmGdpziTrhSzbyfpZsydzp1rfGGgPHqEW` |
| Replace provider after funding | **reverted** — `logistics provider is fixed before funding`; `C4nTu67CaQrGVUVqhwCzTs1q5tzxdHqUnuoibQoKyfNY` |
| Confirm original provider | `3EDeBWFg6mSdMRsuaEq2ei4TN1rMgFjKZamf3rikjRmJ` |
| Seller opens dispute before dispatch | `8D3SfdDpTjsZdeMoZSfxiYuZZfN4mVPHhxVMajtyLTJR` |
| Buyer refunds after dispatch deadline | `2vkxDjV4EAPdyj4sFudGPVd5uwJfkuJ8SabPAzqAd3RN` |

Final state `Refunded`; no shipment was registered and no logistics/seller/platform allocation was
paid. `dispute_time_ms` returned the opening time, and the buyer's refund counter moved to one only
after the refund transfer succeeded.

## Deployment

- Account creation: `E1JJXEmq8GCoWzzJdGpDPEmWwN4kqQZXCHKeEhLHY9sZ`
- WASM deploy: `5A9N45ANcbZfVTA9UKs1YjebuUFdk47jLYAfSbWNfoCp`
- Initialization: `DyosJCcz3AGPDRiBh3vkJCo6kC3eophK9m4LQDA5rdG1`
- Board treasury configuration: `BpPF2u1KTknSXtUW6TGejMuPeXLnAwkzRaaeKXXVfQ6x`
- Escrow deploy-key deletion: `GPA8AfRemcEgnkVCL8CtwZJ6AcHaT18Q5yfeqwbdJkDg`
- Deployed and locally built WASM SHA-256 both:
  `2a0e388507a3ea001442d66dc2e58dc43c2abae6990bfcc446b312b28067f1ba`
- `view_access_key_list` on `sweeft-pay.testnet` returns an empty list.
- Review board `sweeft-board.testnet` remains keyless and council-governed at 2-of-3.

## Exploit-path rejections

| Attempt | On-chain result |
|---|---|
| Provider replacement after funding | reverted: `logistics provider is fixed before funding` |
| `vote_dispute` / `finalize_arbitration` with a review board set | reverted: `arbitration pool is disabled while a review board is set` |
| Owner retaking the board via `set_arbiter(owner)` | reverted: `board already set — only the current board can change it` |
| `withdraw(amount, to)` | `MethodNotFound` — method removed |
| `create_escrow` without a storage deposit | reverted: `attach at least 0.02 NEAR storage deposit` |
| `create_escrow` with an unlisted token | reverted: `token not allowed` |
