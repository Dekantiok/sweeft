#!/usr/bin/env bash
# VeriPay testnet deployment — native-NEAR edition.
# Run from the repository root. Requires: rustup wasm targets, cargo-near,
# near-cli-rs, outlayer CLI, and a funded testnet account.
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

DEPLOYER="${DEPLOYER:-steepdata3968.testnet}"
DEPLOYER_KEY="${DEPLOYER_KEY:?set DEPLOYER_KEY=ed25519:...}"
VERIPAY_ACCOUNT="${VERIPAY_ACCOUNT:-veripay.${DEPLOYER}}"
BUDGET_ACCOUNT="${BUDGET_ACCOUNT:-budget.${DEPLOYER}}"
AGENT_ACCOUNT="${AGENT_ACCOUNT:-agent.${DEPLOYER}}"
RELAYER_ACCOUNT="${RELAYER_ACCOUNT:-relayer.${DEPLOYER}}"
OUTLAYER_CONTRACT="${OUTLAYER_CONTRACT:-outlayer.testnet}"
OUTLAYER_PROJECT="${OUTLAYER_PROJECT:-${DEPLOYER}/veripay-verify}"
PLATFORM_FEE_BPS="${PLATFORM_FEE_BPS:-500}"

sign() { network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send; }

echo "== 1. Build contracts"
(cd contracts/veripay && cargo near build non-reproducible-wasm --no-abi)
(cd contracts/near-budget && cargo near build non-reproducible-wasm --no-abi)

echo "== 2. Build the OutLayer verification module (WASI Preview 2)"
(cd wasi/veripay-verify && cargo build --target wasm32-wasip2 --release)

echo "== 3. Create subaccounts (skip if they exist)"
for acct in "$VERIPAY_ACCOUNT" "$BUDGET_ACCOUNT" "$AGENT_ACCOUNT" "$RELAYER_ACCOUNT"; do
  if ! near account view-account "$acct" network-config testnet now >/dev/null 2>&1; then
    near account create-account fund-myself "$acct" '2 NEAR' autogenerate-new-keypair save-to-keychain \
      sign-as "$DEPLOYER" network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send
  fi
done

echo "== 4. Deploy + init VeriPay escrow (native NEAR by default)"
near contract deploy "$VERIPAY_ACCOUNT" use-file contracts/veripay/target/near/veripay.wasm \
  without-init-call network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send
near contract call-function as-transaction "$VERIPAY_ACCOUNT" new \
  json-args "{\"owner\":\"$DEPLOYER\",\"outlayer_contract\":\"$OUTLAYER_CONTRACT\",\"outlayer_project\":\"$OUTLAYER_PROJECT\",\"platform_fee_bps\":$PLATFORM_FEE_BPS}" \
  prepaid-gas '100 Tgas' attached-deposit '0 NEAR' sign-as "$VERIPAY_ACCOUNT" network-config testnet sign-with-legacy-keychain send

echo "== 5. Deploy + init the native co-pilot budget"
near contract deploy "$BUDGET_ACCOUNT" use-file contracts/near-budget/target/near/near_budget.wasm \
  without-init-call network-config testnet sign-with-legacy-keychain --signer-public-key "$DEPLOYER_KEY" send
near contract call-function as-transaction "$BUDGET_ACCOUNT" new \
  json-args "{\"agent\":\"$AGENT_ACCOUNT\"}" \
  prepaid-gas '100 Tgas' attached-deposit '0 NEAR' sign-as "$BUDGET_ACCOUNT" network-config testnet sign-with-legacy-keychain send

echo "== 6. Publish the TEE verification module to OutLayer"
printf '%s\n%s\n' "$DEPLOYER" "$DEPLOYER_KEY" | outlayer login testnet
WASM=wasi/veripay-verify/target/wasm32-wasip2/release/veripay-verify.wasm
outlayer upload "$WASM"
# capture the FastFS URL printed above, then:
# outlayer deploy veripay-verify <FastFS URL>

# Optional legacy NEP-141 support for escrows created before the native migration:
# (cd contracts/test-token && cargo near build non-reproducible-wasm --no-abi)
# deploy contracts/test-token/target/near/test_token.wasm to tusd.${DEPLOYER} and
# set TUSD_CONTRACT accordingly. New escrows do NOT need it.

echo "Done. Set VERIPAY_CONTRACT=$VERIPAY_ACCOUNT AI_BUDGET_CONTRACT=$BUDGET_ACCOUNT OUTLAYER_PROJECT=$OUTLAYER_PROJECT"
echo "      RELAYER_ACCOUNT=$RELAYER_ACCOUNT RELAYER_ENABLED=1 (key at ~/.near-credentials/testnet/) in veripay/.env"
