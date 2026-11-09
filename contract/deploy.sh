#!/usr/bin/env bash
# Deploy escrow contract to testnet. Run from repo root: bash contract/deploy.sh
# Needs: npm i -g near-cli, testnet account with key in ~/.near-credentials/testnet/<ACCOUNT>.json
set -euo pipefail
ACCOUNT="${ESCROW_CONTRACT:-sweeft-tbd.testnet}"
NETWORK="${NETWORK:-testnet}"
cd "$(dirname "$0")"
echo "== build =="
npm install
npm run build || { echo "build needs near-sdk-js toolchain; see contract/package.json"; exit 1; }
WASM="build/escrow.wasm"
[ -f "$WASM" ] || { echo "missing $WASM"; exit 1; }
echo "== deploy to $ACCOUNT ($NETWORK) =="
near deploy --accountId "$ACCOUNT" --wasmFile "$WASM" --networkId "$NETWORK"
echo "deployed. Update ESCROW_CONTRACT=$ACCOUNT in .env and README."
