#!/usr/bin/env bash
# SWEEFT end-to-end testnet lifecycle (reference + demo script).
# Runs: create -> accept -> fund (native NEAR) -> assign provider ->
# register shipment -> OutLayer TEE verification -> settle logistics ->
# confirm receipt. Prints native NEAR balances at each settlement step.
#
# Usage:  export SELLER_KEY=... BUYER_KEY=... ; scripts/e2e_lifecycle.sh
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

SWEEFT="${SWEEFT:-sweeft-final2.testnet}"
SELLER="${SELLER:-seller.steepdata3968.testnet}"
BUYER="${BUYER:-steepdata3968.testnet}"
PROVIDER="${PROVIDER:-logistics.steepdata3968.testnet}"
SELLER_KEY="${SELLER_KEY:?set SELLER_KEY=ed25519:...}"
BUYER_KEY="${BUYER_KEY:?set BUYER_KEY=ed25519:...}"
NEAR_RPC="${NEAR_RPC_URL:-https://test.rpc.fastnear.com}"
SELLER_AMOUNT="${SELLER_AMOUNT:-100000000000000000000000}"      # 0.1 NEAR (yocto)
LOGISTICS_AMOUNT="${LOGISTICS_AMOUNT:-10000000000000000000000}" # 0.01 NEAR
# yoctoNEAR exceeds 64-bit shell arithmetic, so compute with Python.
PLATFORM_FEE=$(python3 -c "print($SELLER_AMOUNT * 500 // 10000)") # 5% enforced by the contract
TOTAL=$(python3 -c "print($SELLER_AMOUNT + $LOGISTICS_AMOUNT + $PLATFORM_FEE)") # 0.115 NEAR

call() { # account key method args [deposit] [gas]
  near contract call-function as-transaction "$SWEEFT" "$3" json-args "$4" \
    prepaid-gas "${6:-50} Tgas" attached-deposit "${5:-0 NEAR}" sign-as "$1" \
    network-config testnet sign-with-legacy-keychain --signer-public-key "$2" send
}
bal() { # native NEAR balance
  curl -s "$NEAR_RPC" -H 'Content-Type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"query\",\"params\":{\"request_type\":\"view_account\",\"finality\":\"final\",\"account_id\":\"$1\"}}" \
    | python3 -c "import json,sys; print(round(int(json.load(sys.stdin)['result']['amount'])/1e24, 6))"
}

DL=$(( ($(date +%s) + 3600) * 1000 ))
TRACKING="DEMO-E2E-$(date +%s | tail -c 5)"

echo "== create escrow (seller, native NEAR)"
ESCROW_ID=$(call "$SELLER" "$SELLER_KEY" create_escrow \
  "{\"product_name\":\"E2E test goods\",\"product_desc_hash\":\"e2e\",\"seller_amount\":\"$SELLER_AMOUNT\",\"logistics_amount\":\"$LOGISTICS_AMOUNT\",\"buyer\":\"$BUYER\",\"invite_hash\":null,\"token\":\"near\",\"dispatch_deadline_ms\":$DL,\"inspection_period_ms\":172800000,\"terms_hash\":\"e2e-terms\",\"logistics_provider\":null,\"tracking_number\":null}" \
  '0.1 NEAR' | grep -o 'SWFT-[0-9]*' | head -1)
echo "   escrow: $ESCROW_ID"

echo "== accept (buyer)"; call "$BUYER" "$BUYER_KEY" accept_escrow "{\"escrow_id\":\"$ESCROW_ID\",\"invite_secret\":null}" '0 NEAR'
echo "== fund $TOTAL yoctoNEAR (native)"
call "$BUYER" "$BUYER_KEY" fund_escrow "{\"escrow_id\":\"$ESCROW_ID\"}" "$TOTAL yoctoNEAR"
echo "== assign provider + register shipment (seller)"
call "$SELLER" "$SELLER_KEY" assign_logistics_provider "{\"escrow_id\":\"$ESCROW_ID\",\"provider\":\"$PROVIDER\"}" '0 NEAR' >/dev/null
call "$SELLER" "$SELLER_KEY" register_shipment "{\"escrow_id\":\"$ESCROW_ID\",\"tracking_number\":\"$TRACKING\"}" '0 NEAR' >/dev/null

echo "== TEE verification via OutLayer"
# Signed carrier payload (mandatory: the TEE verifies the carrier signature).
PAYLOAD=$(python3 scripts/carrier_payload.py "$ESCROW_ID")
REQ_ARGS="{\"escrow_id\":\"$ESCROW_ID\",\"carrier_payload\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$PAYLOAD")}"
call "$SELLER" "$SELLER_KEY" request_delivery_verification "$REQ_ARGS" '0.05 NEAR' 200 >/dev/null
echo "   waiting for the OutLayer TEE callback (polling up to 4 minutes)…"
for i in $(seq 1 24); do
  sleep 10
  VERIFIED=$(near contract call-function as-read-only "$SWEEFT" get_escrow \
    json-args "{\"escrow_id\":\"$ESCROW_ID\"}" network-config testnet now 2>/dev/null \
    | grep -o '"delivered_verified": *[a-z]*' | awk '{print $2}' | head -1)
  if [ "$VERIFIED" = "true" ]; then echo "   TEE verified (poll $i)"; break; fi
  PENDING=$(near contract call-function as-read-only "$SWEEFT" get_escrow \
    json-args "{\"escrow_id\":\"$ESCROW_ID\"}" network-config testnet now 2>/dev/null \
    | grep -o '"verification_pending": *[a-z]*' | awk '{print $2}' | head -1)
  if [ "$PENDING" = "false" ] && [ "$VERIFIED" != "true" ]; then
    echo "   TEE run returned no outcome — re-requesting (poll $i)"
    call "$SELLER" "$SELLER_KEY" request_delivery_verification "$REQ_ARGS" '0.05 NEAR' 200 >/dev/null || true
  fi
done
[ "${VERIFIED:-}" = "true" ] || { echo "TEE verification did not complete"; exit 1; }
echo "== settle logistics"
echo "   logistics before: $(bal "$PROVIDER")"
call "$SELLER" "$SELLER_KEY" settle_logistics "{\"escrow_id\":\"$ESCROW_ID\"}" '0 NEAR' >/dev/null
sleep 4
echo "   logistics after:  $(bal "$PROVIDER")"
echo "== confirm receipt (buyer)"
echo "   seller before: $(bal "$SELLER")"
call "$BUYER" "$BUYER_KEY" confirm_receipt "{\"escrow_id\":\"$ESCROW_ID\"}" '0 NEAR' >/dev/null
sleep 5
echo "   seller after:  $(bal "$SELLER")"
echo "   treasury:      $(bal "sweeft-board.testnet")"
echo "== state:"
near contract call-function as-read-only "$SWEEFT" get_escrow json-args "{\"escrow_id\":\"$ESCROW_ID\"}" network-config testnet now 2>&1 | grep -E '"(state|logistics_paid|seller_paid|platform_paid|delivered_verified|pickup_verified)"'
