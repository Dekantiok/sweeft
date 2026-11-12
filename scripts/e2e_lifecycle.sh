#!/usr/bin/env bash
# VeriPay end-to-end testnet lifecycle (reference + demo script).
# Runs: create -> accept -> fund (tUSD ft_transfer_call) -> assign provider ->
# register shipment -> OutLayer TEE verification -> settle logistics ->
# confirm receipt. Prints balances at each settlement step.
#
# Usage:  export SELLER_KEY=... BUYER_KEY=... DELEGATE... ; scripts/e2e_lifecycle.sh
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

VERIPAY="${VERIPAY:-veripay.steepdata3968.testnet}"
TUSD="${TUSD:-tusd.steepdata3968.testnet}"
SELLER="${SELLER:-seller.steepdata3968.testnet}"
BUYER="${BUYER:-steepdata3968.testnet}"
PROVIDER="${PROVIDER:-logistics.steepdata3968.testnet}"
SELLER_KEY="${SELLER_KEY:?set SELLER_KEY=ed25519:...}"
BUYER_KEY="${BUYER_KEY:?set BUYER_KEY=ed25519:...}"
SELLER_AMOUNT="${SELLER_AMOUNT:-100000000}"      # 100 tUSD (6 decimals)
LOGISTICS_AMOUNT="${LOGISTICS_AMOUNT:-10000000}"  # 10 tUSD
PLATFORM_FEE=$(( SELLER_AMOUNT * 500 / 10000 ))   # 5% enforced by the contract
TOTAL=$(( SELLER_AMOUNT + LOGISTICS_AMOUNT + PLATFORM_FEE ))

call() { # account key method args [deposit] [gas]
  near contract call-function as-transaction "$VERIPAY" "$3" json-args "$4" \
    prepaid-gas "${6:-100} Tgas" attached-deposit "${5:-0 NEAR}" sign-as "$1" \
    network-config testnet sign-with-legacy-keychain --signer-public-key "$2" send
}
bal() { near contract call-function as-read-only "$TUSD" ft_balance_of \
  json-args "{\"account_id\":\"$1\"}" network-config testnet now 2>&1 | grep -A1 "return value" | tail -1 | tr -d '"'; }

DL=$(( ($(date +%s) + 3600) * 1000 ))
TRACKING="DEMO-E2E-$(date +%s | tail -c 5)"

echo "== create escrow (seller)"
ESCROW_ID=$(call "$SELLER" "$SELLER_KEY" create_escrow \
  "{\"product_name\":\"E2E test goods\",\"product_desc_hash\":\"e2e\",\"seller_amount\":\"$SELLER_AMOUNT\",\"logistics_amount\":\"$LOGISTICS_AMOUNT\",\"buyer\":\"$BUYER\",\"invite_hash\":null,\"token\":\"$TUSD\",\"dispatch_deadline_ms\":$DL,\"inspection_period_ms\":172800000,\"terms_hash\":\"e2e-terms\"}" \
  '0.1 NEAR' | grep -o 'VP-[0-9]*' | head -1)
echo "   escrow: $ESCROW_ID"

echo "== accept (buyer)"; call "$BUYER" "$BUYER_KEY" accept_escrow "{\"escrow_id\":\"$ESCROW_ID\",\"invite_secret\":null}" '0 NEAR'
echo "== fund $TOTAL tUSD"; 
near contract call-function as-transaction "$TUSD" ft_transfer_call json-args \
  "{\"receiver_id\":\"$VERIPAY\",\"amount\":\"$TOTAL\",\"msg\":\"$ESCROW_ID\",\"memo\":null}" \
  prepaid-gas '100 Tgas' attached-deposit '1 yoctoNEAR' sign-as "$BUYER" network-config testnet \
  sign-with-legacy-keychain --signer-public-key "$BUYER_KEY" send >/dev/null
echo "== assign provider + register shipment (seller)"
call "$SELLER" "$SELLER_KEY" assign_logistics_provider "{\"escrow_id\":\"$ESCROW_ID\",\"provider\":\"$PROVIDER\"}" '0 NEAR' >/dev/null
call "$SELLER" "$SELLER_KEY" register_shipment "{\"escrow_id\":\"$ESCROW_ID\",\"tracking_number\":\"$TRACKING\"}" '0 NEAR' >/dev/null

echo "== TEE verification via OutLayer"
PAYLOAD=$(python3 - <<PY
import json
carrier = {"provider_id":"LOG-DEMO-001","tracking_number":"$TRACKING","status":"DELIVERED",
           "events":[{"status":"PICKED_UP"},{"status":"DELIVERED"}]}
print(json.dumps({"escrow_id":"$ESCROW_ID","carrier_payload":json.dumps(carrier)}))
PY
)
call "$SELLER" "$SELLER_KEY" request_delivery_verification "$PAYLOAD" '0.05 NEAR' 200 >/dev/null
sleep 18
echo "== settle logistics"
echo "   logistics before: $(bal "$PROVIDER")"
call "$SELLER" "$SELLER_KEY" settle_logistics "{\"escrow_id\":\"$ESCROW_ID\"}" '0 NEAR' >/dev/null
sleep 4
echo "   logistics after:  $(bal "$PROVIDER")"
echo "== confirm receipt (buyer)"
echo "   seller before: $(bal "$SELLER") platform before: $(bal "$BUYER")"
call "$BUYER" "$BUYER_KEY" confirm_receipt "{\"escrow_id\":\"$ESCROW_ID\"}" '0 NEAR' >/dev/null
sleep 5
echo "   seller after:  $(bal "$SELLER")"
echo "== state:"
call '' '' get_escrow "{\"escrow_id\":\"$ESCROW_ID\"}" 2>/dev/null || true
near contract call-function as-read-only "$VERIPAY" get_escrow json-args "{\"escrow_id\":\"$ESCROW_ID\"}" network-config testnet now 2>&1 | grep -E '"(state|logistics_paid|seller_paid|platform_paid|delivered_verified|pickup_verified)"'
