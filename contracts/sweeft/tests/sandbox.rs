//! Sandbox lifecycle coverage for the SWEEFT escrow contract.
//!
//! Build both wasm artifacts first, then run the test:
//! `cargo near build non-reproducible-wasm --no-abi` (or a plain
//! `cargo build --release --target wasm32-unknown-unknown`) in this crate, and
//! `cargo build --release --target wasm32-unknown-unknown` in
//! `../stub-outlayer`; then `cargo test --test sandbox`.
//! The test deploys the sandbox-only stub OutLayer runner
//! (`contracts/stub-outlayer`) so `request_delivery_verification` runs its
//! real promise callback into `on_verification_result` — the same code path the
//! live OutLayer TEE uses, with no TEE in the loop.

use near_workspaces::types::{Gas, NearToken};
use serde_json::{json, Value};

const WASM_PATHS: [&str; 2] = [
    "target/near/sweeft.wasm",
    "target/wasm32-unknown-unknown/release/sweeft.wasm",
];
const STUB_WASM_PATHS: [&str; 2] = [
    "../stub-outlayer/target/wasm32-unknown-unknown/release/stub_outlayer.wasm",
    "../stub-outlayer/target/near/stub_outlayer.wasm",
];

fn read_contract_wasm() -> Vec<u8> {
    for path in WASM_PATHS {
        if let Ok(bytes) = std::fs::read(path) {
            return bytes;
        }
    }
    panic!("build the contract wasm before running sandbox tests (cargo near build non-reproducible-wasm --no-abi)");
}

fn read_stub_wasm() -> Vec<u8> {
    for path in STUB_WASM_PATHS {
        if let Ok(bytes) = std::fs::read(path) {
            return bytes;
        }
    }
    panic!(
        "build the stub OutLayer runner first: cargo build --release --target wasm32-unknown-unknown --manifest-path ../stub-outlayer/Cargo.toml"
    );
}
const SELLER_AMOUNT: u128 = 10_000_000_000_000_000_000_000; // 0.01 NEAR
const LOGISTICS_AMOUNT: u128 = 1_000_000_000_000_000_000_000; // 0.001 NEAR
const PLATFORM_FEE: u128 = 500_000_000_000_000_000_000; // 0.0005 NEAR
const STORAGE_DEPOSIT: u128 = 20_000_000_000_000_000_000_000; // 0.02 NEAR
const VERIFICATION_DEPOSIT: u128 = 1_000_000_000_000_000_000_000; // 0.001 NEAR

async fn view(
    contract: &near_workspaces::Contract,
    method: &str,
    args: Value,
) -> near_workspaces::Result<Value> {
    contract.call(method).args_json(args).view().await?.json()
}

#[tokio::test]
async fn full_lifecycle_completed_escrow_leaves_active_index() -> near_workspaces::Result<()> {
    let worker = near_workspaces::sandbox().await?;
    let wasm = read_contract_wasm();
    let stub_wasm = read_stub_wasm();
    let contract = worker.dev_deploy(&wasm).await?;
    let outlayer = worker.dev_deploy(&stub_wasm).await?;
    outlayer.call("new").transact().await?.into_result()?;
    let seller = worker.dev_create_account().await?;
    let buyer = worker.dev_create_account().await?;
    let provider = worker.dev_create_account().await?;
    let board = worker.dev_create_account().await?;

    let now_ms = worker.view_block().await?.timestamp() / 1_000_000;
    let dispatch_deadline_ms = now_ms + 7 * 24 * 60 * 60 * 1_000;

    contract
        .call("new")
        .args_json(json!({
            "owner": contract.id().to_string(),
            "outlayer_contract": outlayer.id().to_string(),
            "outlayer_project": "sandbox.testnet/sweeft-verify",
            "platform_fee_bps": 500,
            "arbiter": board.id().to_string(),
            "provider_id": "LOG-DEMO-001",
            "carrier_pubkey": "ab".repeat(32),
        }))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    let total_amount = SELLER_AMOUNT + LOGISTICS_AMOUNT + PLATFORM_FEE;
    let escrow_id: String = seller
        .call(contract.id(), "create_escrow")
        .args_json(json!({
            "product_name": "Sandbox escrow",
            "product_desc_hash": "sandbox-hash",
            "seller_amount": SELLER_AMOUNT.to_string(),
            "logistics_amount": LOGISTICS_AMOUNT.to_string(),
            "buyer": buyer.id().to_string(),
            "invite_hash": null,
            "token": "near",
            "dispatch_deadline_ms": dispatch_deadline_ms,
            "inspection_period_ms": 3_600_000,
            "terms_hash": "sandbox-terms",
            "logistics_provider": provider.id().to_string(),
            "tracking_number": "SANDBOX-1",
        }))
        .deposit(NearToken::from_yoctonear(STORAGE_DEPOSIT))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .json()?;

    buyer
        .call(contract.id(), "accept_escrow")
        .args_json(json!({ "escrow_id": escrow_id, "invite_secret": null }))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    buyer
        .call(contract.id(), "fund_escrow")
        .args_json(json!({ "escrow_id": escrow_id }))
        .deposit(NearToken::from_yoctonear(total_amount))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    seller
        .call(contract.id(), "assign_logistics_provider")
        .args_json(json!({
            "escrow_id": escrow_id,
            "provider": provider.id().to_string(),
        }))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    seller
        .call(contract.id(), "register_shipment")
        .args_json(json!({ "escrow_id": escrow_id, "tracking_number": "SANDBOX-1" }))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    let shipped = view(&contract, "get_escrow", json!({ "escrow_id": escrow_id })).await?;
    assert_eq!(shipped["state"], Value::String("Shipped".to_string()));

    // Drive verification through the real promise chain: the stub OutLayer
    // runner answers `request_execution`, and the escrow contract's private
    // `on_verification_result` callback reads the promise result.
    seller
        .call(contract.id(), "request_delivery_verification")
        .args_json(json!({
            "escrow_id": escrow_id,
            "carrier_payload": json!({
                "provider_id": "LOG-DEMO-001",
                "tracking_number": "SANDBOX-1",
                "shipment_id": format!("SHIP-{escrow_id}"),
                "status": "DELIVERED",
                "events": [{"status": "PICKED_UP"}, {"status": "DELIVERED"}],
            })
            .to_string(),
        }))
        .deposit(NearToken::from_yoctonear(VERIFICATION_DEPOSIT))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    let verified = view(&contract, "get_escrow", json!({ "escrow_id": escrow_id })).await?;
    assert_eq!(
        verified["state"],
        Value::String("AwaitingBuyerConfirmation".to_string())
    );
    assert_eq!(verified["pickup_verified"], Value::Bool(true));
    assert_eq!(verified["delivered_verified"], Value::Bool(true));
    assert_eq!(verified["verification_pending"], Value::Bool(false));

    provider
        .call(contract.id(), "settle_logistics")
        .args_json(json!({ "escrow_id": escrow_id }))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    buyer
        .call(contract.id(), "confirm_receipt")
        .args_json(json!({ "escrow_id": escrow_id }))
        .gas(Gas::from_tgas(300))
        .transact()
        .await?
        .into_result()?;

    let completed = view(&contract, "get_escrow", json!({ "escrow_id": escrow_id })).await?;
    assert_eq!(completed["state"], Value::String("Completed".to_string()));
    assert_eq!(completed["seller_paid"], Value::Bool(true));
    assert_eq!(completed["logistics_paid"], Value::Bool(true));
    assert_eq!(completed["platform_paid"], Value::Bool(true));

    // Terminal escrows no longer count against the active account index cap.
    let active_for_seller = view(
        &contract,
        "escrows_for",
        json!({ "account_id": seller.id().to_string(), "from_index": 0, "limit": 50 }),
    )
    .await?;
    assert_eq!(active_for_seller.as_array().unwrap().len(), 0);

    // History remains available through the creation-order range view.
    let history = view(
        &contract,
        "escrows_range",
        json!({ "start": 1, "limit": 1 }),
    )
    .await?;
    assert_eq!(
        history.as_array().unwrap()[0]["id"],
        Value::String(escrow_id.clone())
    );
    let count = view(&contract, "escrow_count", json!({})).await?;
    assert_eq!(count, Value::Number(1.into()));

    Ok(())
}
