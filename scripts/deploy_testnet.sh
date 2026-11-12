#!/usr/bin/env bash
# VeriPay testnet deployment — reference script.
# Run from the repository root. Requires: rustup wasm targets, cargo-near,
# near-cli-rs, outlayer CLI, and a funded testnet account.
set -euo pipefail

export PATH="$HOME/.cargo/bin:$PATH"

# ---------------------------------------------------------------------------
# Configuration (edit before running)
# ---------------------------------------------------------------------------
DEPLOYER="${DEPLOYER:-steepdata3968.testnet}"          # parent account (pays, owns platform fee)
DEPLOYER_KEY="${DEPLOYER_KEY:?set DEPLOYER_KEY=ed25519:...}"
TUSD_ACCOUNT="${TUSD_ACCOUNT:-tusd.${DEPLOYER}}"
VERIPAY_ACCOUNT="${VERIPAY_ACCOUNT:-veripay.${DEPLOYER}}"
OUTLAYER_CONTRACT="${OUTLAYER_CONTRACT:-outlayer.testnet}"
OUTLAYER_PROJECT="${OUTLAYER_PROJECT:-${DEPLOYER}/veripay-verify}"
PLATFORM_FEE_BPS="${PLATFORM_FEE_BPS:-500}"

sign() { network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send; }

echo "== 1. Build contracts"
(cd contracts/test-token && cargo near build non-reproducible-wasm --no-abi)
(cd contracts/veripay && cargo near build non-reproducible-wasm --no-abi)

echo "== 2. Build the OutLayer verification module (WASI Preview 2)"
(cd wasi/veripay-verify && cargo build --target wasm32-wasip2 --release)

echo "== 3. Create subaccounts (skip if they exist)"
for acct in "$TUSD_ACCOUNT" "$VERIPAY_ACCOUNT"; do
  if ! near account view-account "$acct" network-config testnet now >/dev/null 2>&1; then
    near account create-account fund-myself "$acct" '2 NEAR' autogenerate-new-keypair save-to-keychain \
      sign-as "$DEPLOYER" network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send
  fi
done

echo "== 4. Deploy + init tUSD"
near contract deploy "$TUSD_ACCOUNT" use-file contracts/test-token/target/near/test_token.wasm without-init-call $(: ) network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send
near contract call-function as-transaction "$TUSD_ACCOUNT" new \
  json-args '{"name":"VeriPay Test Dollar","symbol":"tUSD","decimals":6}' \
  prepaid-gas '100 Tgas' attached-deposit '0 NEAR' sign-as "$TUSD_ACCOUNT" $(: ) network-config testnet sign-with-legacy-keychain send

echo "== 5. Deploy + init VeriPay escrow"
near contract deploy "$VERIPAY_ACCOUNT" use-file contracts/veripay/target/near/veripay.wasm without-init-call $(: ) network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send
near contract call-function as-transaction "$VERIPAY_ACCOUNT" new \
  json-args "{\"owner\":\"$DEPLOYER\",\"outlayer_contract\":\"$OUTLAYER_CONTRACT\",\"outlayer_project\":\"$OUTLAYER_PROJECT\",\"platform_fee_bps\":$PLATFORM_FEE_BPS}" \
  prepaid-gas '100 Tgas' attached-deposit '0 NEAR' sign-as "$VERIPAY_ACCOUNT" $(: ) network-config testnet sign-with-legacy-keychain send

echo "== 6. Register the escrow contract in tUSD storage"
near contract call-function as-transaction "$TUSD_ACCOUNT" storage_deposit \
  json-args "{\"account_id\":\"$VERIPAY_ACCOUNT\",\"registration_only\":true}" \
  prepaid-gas '50 Tgas' attached-deposit '0.01 NEAR' sign-as "$DEPLOYER" $(: ) network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send

echo "== 7. Publish the TEE verification module to OutLayer"
printf '%s\n%s\n' "$DEPLOYER" "$DEPLOYER_KEY" | outlayer login testnet
WASM=wasi/veripay-verify/target/wasm32-wasip2/release/veripay-verify.wasm
outlayer upload "$WASM"
# capture the FastFS URL printed above, then:
# outlayer deploy veripay-verify <FastFS URL>

echo "== 8. Mint demo tokens"
near contract call-function as-transaction "$TUSD_ACCOUNT" mint \
  json-args "{\"account_id\":\"$DEPLOYER\",\"amount\":\"500000000000\"}" \
  prepaid-gas '50 Tgas' attached-deposit '0 NEAR' sign-as "$TUSD_ACCOUNT" $(: ) network-config testnet sign-with-legacy-keychain send

echo "Done. Set VERIPAY_CONTRACT=$VERIPAY_ACCOUNT TUSD_CONTRACT=$TUSD_ACCOUNT OUTLAYER_PROJECT=$OUTLAYER_PROJECT in veripay/.env"
