# Verified: end-to-end on NEAR testnet

Current deployment: escrow **`sweeft.testnet`**, review board **`sweeft-board.testnet`** (both
keyless/immutable — deploy keys deleted), TEE project **`steepdata3968.testnet/sweeft-verify`**,
carrier signing key registered on-chain.

## Lifecycle — `SWFT-100185` → `Completed`

| Step | Tx |
|---|---|
| `create_escrow` (seller) | `bFB5W1NodeuLj1Dz213LwwA5gmhNuMwVYBRQNHjStAL` |
| `accept_escrow` (buyer) | `AsarhniZo3mCAdWnGNEDT3qqySjyMWGdsXcgNcN4mvDL` |
| `fund_escrow` 0.115 NEAR | `UWkMh74iWrfK8AkcnD6E8T7ohErk9EGHFyCops4G8Du` |
| `assign_logistics_provider` | `CJVJdpc2wchpVeQ11Uh9x7VjqTPke96E38vNgNX7S6xL` |
| `register_shipment` | `PVAyY248s5QpXjVABtYnMMgWKgYu4mmTJnRdNPnJmJg` |
| TEE verification (signed carrier payload) | `9QmhQesGSnzFAh9zjnpycFKsnxfjcEadPVMvu1mDXwM9` |
| `settle_logistics` | `C8cFZWwnEjdy2hHnkRrhfdkUoxZizm8PY5LU491pJNpo` |
| `confirm_receipt` | `7rHJAMq5qRTkJChH4qFb3g17MScVotvkepL9yK5haAA2` |

Final state `Completed`; seller, logistics and platform payout flags all true. Payout deltas:
seller **+0.1 NEAR**, logistics **+0.01 NEAR**, platform fee **+0.005 NEAR** to the board treasury.

## Deployment

- escrow `sweeft.testnet` — deploy `G1oX2naj11NEKVjbpeAzJzuDDARWTvRPqWy83Fj3m25E`, init
  `2BETaackYkgBRHKw3v5dxdDzz4wvfmUqafowhJrSiKxS`, treasury
  `8gyr5oT9RbXmCMb4JUkqWRo1xCPbn1jP2XrE9c7GM6BD`
- deploy key deleted (tx `Eb8CqtNYt8zNK2RzfmqYcjzduRsTDcCBz3HZxqntaekX`) — `view_access_key_list`
  returns empty
- review board `sweeft-board.testnet` — keyless (tx `BYxu9F3GcHhUxVKE8Qe7ufnRwEbVVJK8mZd5FKb9bTk6`),
  council 2-of-3

## Exploit-path rejections (rejected live)

| Attempt | On-chain result |
|---|---|
| `vote_dispute` / `finalize_arbitration` with a review board set | reverted: `arbitration pool is disabled while a review board is set` |
| Owner retaking the board via `set_arbiter(owner)` | reverted: `board already set — only the current board can change it` |
| `withdraw(amount, to)` | `MethodNotFound` — method removed |
| `create_escrow` without a storage deposit | reverted: `attach at least 0.02 NEAR storage deposit` |
| `create_escrow` with an unlisted token | reverted: `token not allowed` |

## App checks

- an authenticated NEP-413 session renders `/`, `/dashboard/`, `/escrow/SWFT-100185/` and
  `/governance/` with HTTP 200 and shows the current contract address.
