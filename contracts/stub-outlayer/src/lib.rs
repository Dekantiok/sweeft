//! Sandbox-only stub for the OutLayer execution service.
//!
//! The SWEEFT sandbox test deploys this contract in place of the real
//! `outlayer.testnet` project runner, so the escrow contract's promise
//! callback path (`request_delivery_verification` -> `on_verification_result`)
//! is exercised end-to-end without a TEE. It derives a verified DELIVERED
//! outcome from the very input the escrow contract sent, which is exactly the
//! shape the real verifier returns.
//!
//! This crate is never deployed outside the local sandbox.

use near_sdk::near;
use serde_json::{json, Value};

#[near(contract_state)]
#[derive(Default)]
pub struct StubOutlayer;

#[near]
impl StubOutlayer {
    #[init]
    pub fn new() -> Self {
        Self
    }

    pub fn request_execution(
        &self,
        source: Value,
        resource_limits: Value,
        input_data: String,
        response_format: String,
    ) -> Value {
        let _ = (source, resource_limits, response_format);
        let input: Value = serde_json::from_str(&input_data).unwrap_or(Value::Null);
        let escrow_id = input
            .get("escrow_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let expected = input.get("expected").cloned().unwrap_or(Value::Null);
        let tracking = expected
            .get("tracking_number")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let provider = expected
            .get("provider_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        json!({
            "escrow_id": escrow_id,
            "tracking_number": tracking,
            "provider_id": provider,
            "reported_status": "DELIVERED",
            "verified": true,
            "reason": "sandbox stub: signed carrier report matched the registered shipment",
        })
    }
}
