//! Sandbox lifecycle coverage for the SWEEFT escrow contract.
//!
//! The contract source is built to `target/near/sweeft.wasm` before running:
//! `cargo near build reproducible-wasm --no-abi && cargo test --test sandbox`.
//! The sandbox does not run the OutLayer TEE; `on_verification_result` is
//! invoked as the contract itself, which is exactly the predecessor allowed by
//! the `#[private]` callback guard.

use near_workspaces::types::NearToken;
use serde_json::{json, Value};

const WASM_PATH: &str = "target/near/sweeft.wasm";
const SELLER_AMOUNT: u128 = 10_000_000_000_000_000_000_000; // 0.01 NEAR
const LOGISTICS_AMOUNT: u128 = 1_000_000_000_000_000_000_000; // 0.001 NEAR
const PLATFORM_FEE: u128 = 500_000_000_000_000_000_000; // 0.0005 NEAR
const STORAGE_DEPOSIT: u128 = 20_000_000_000_000_000_000_000; // 0.02 NEAR

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
    let wasm =
        std::fs::read(WASM_PATH).expect("build the reproducible WASM before running sandbox tests");
    let contract = worker.dev_deploy(&wasm).await?;
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
            "outlayer_contract": "outlayer.testnet",
            "outlayer_project": "seller.testnet/sweeft-verify",
            "platform_fee_bps": 500,
            "arbiter": board.id().to_string(),
            "provider_id": "LOG-DEMO-001",
            "carrier_pubkey": "ab".repeat(32),
        }))
        .max_gas()
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
        .max_gas()
        .transact()
        .await?
        .json()?;

    buyer
        .call(contract.id(), "accept_escrow")
        .args_json(json!({ "escrow_id": escrow_id, "invite_secret": null }))
        .max_gas()
        .transact()
        .await?
        .into_result()?;

    buyer
        .call(contract.id(), "fund_escrow")
        .args_json(json!({ "escrow_id": escrow_id }))
        .deposit(NearToken::from_yoctonear(total_amount))
        .max_gas()
        .transact()
        .await?
        .into_result()?;

    seller
        .call(contract.id(), "assign_logistics_provider")
        .args_json(json!({
            "escrow_id": escrow_id,
            "provider": provider.id().to_string(),
        }))
        .max_gas()
        .transact()
        .await?
        .into_result()?;

    seller
        .call(contract.id(), "register_shipment")
        .args_json(json!({ "escrow_id": escrow_id, "tracking_number": "SANDBOX-1" }))
        .max_gas()
        .transact()
        .await?
        .into_result()?;

    let shipped = view(&contract, "get_escrow", json!({ "escrow_id": escrow_id })).await?;
    assert_eq!(shipped["state"], Value::String("Shipped".to_string()));

    // Emulate the OutLayer promise callback into the private method.
    contract
        .call("on_verification_result")
        .args_json(json!({
            "escrow_id": escrow_id,
            "result": {
                "Ok": {
                    "Some": {
                        "escrow_id": escrow_id,
                        "tracking_number": "SANDBOX-1",
                        "provider_id": "LOG-DEMO-001",
                        "reported_status": "DELIVERED",
                        "verified": true,
                        "reason": "sandbox verification callback",
                    }
                }
            }
        }))
        .max_gas()
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

    provider
        .call(contract.id(), "settle_logistics")
        .args_json(json!({ "escrow_id": escrow_id }))
        .max_gas()
        .transact()
        .await?
        .into_result()?;

    buyer
        .call(contract.id(), "confirm_receipt")
        .args_json(json!({ "escrow_id": escrow_id }))
        .max_gas()
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
