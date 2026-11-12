//! VeriPay delivery verification module — runs inside OutLayer's Intel TDX TEE.
//!
//! Input (stdin, JSON):
//! {
//!   "escrow_id": "VP-000001",
//!   "expected": { "tracking_number": "...", "provider_wallet": "log.testnet", "shipment_id": "SHIP-..." },
//!   "carrier":  { "provider_id": "LOG-001", "tracking_number": "...", "status": "DELIVERED",
//!                 "events": [ { "status": "PICKED_UP", "timestamp": "2026-11-12T10:00:00Z" }, ... ] }
//! }
//!
//! Output (stdout, exactly one JSON value):
//! { "escrow_id": ..., "tracking_number": ..., "provider_id": ..., "reported_status": "DELIVERED",
//!   "verified": true|false, "reason": "..." }
//!
//! The attestation proves that exactly this code ran on exactly this input.
//! The carrier payload is supplied by the caller (Demo Carrier); real carrier
//! API fetching would be a WASI P2 upgrade, disclosed in the README.

use serde::{Deserialize, Serialize};
use std::io::{self, Read, Write};

#[derive(Deserialize, Debug, Default)]
struct Input {
    #[serde(default)]
    escrow_id: String,
    #[serde(default)]
    expected: Expected,
    #[serde(default)]
    carrier: serde_json::Value,
}

#[derive(Deserialize, Debug, Default)]
struct Expected {
    #[serde(default)]
    tracking_number: Option<String>,
    #[serde(default)]
    provider_wallet: Option<String>,
    #[serde(default)]
    shipment_id: Option<String>,
}

#[derive(Serialize, Debug)]
struct Output {
    escrow_id: String,
    tracking_number: String,
    provider_id: String,
    reported_status: String,
    verified: bool,
    reason: String,
}

const HAPPY_CHAIN: [&str; 6] = [
    "CREATED",
    "ACCEPTED",
    "PICKED_UP",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "DELIVERED",
];
const FAILURE_STATUSES: [&str; 4] = ["DELIVERY_FAILED", "RETURNED", "LOST", "CANCELLED"];
const ALL_STATUSES: [&str; 10] = [
    "CREATED",
    "ACCEPTED",
    "PICKED_UP",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "DELIVERED",
    "DELIVERY_FAILED",
    "RETURNED",
    "LOST",
    "CANCELLED",
];

fn normalize_status(raw: &str) -> Option<String> {
    let key = raw.trim().to_ascii_uppercase().replace([' ', '-'], "_");
    ALL_STATUSES.iter().find(|s| **s == key).map(|s| s.to_string())
}

fn rank(status: &str) -> Option<usize> {
    HAPPY_CHAIN.iter().position(|s| *s == status)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = String::new();
    io::stdin().read_to_string(&mut raw)?;

    let input: Input = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            print_output(Output {
                escrow_id: String::new(),
                tracking_number: String::new(),
                provider_id: String::new(),
                reported_status: String::new(),
                verified: false,
                reason: format!("malformed input json: {e}"),
            })?;
            return Ok(());
        }
    };

    let carrier = &input.carrier;
    let tracking = carrier
        .get("tracking_number")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    let provider_id = carrier
        .get("provider_id")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    let raw_status = carrier.get("status").and_then(|v| v.as_str()).unwrap_or("");

    let fail = |reason: String| Output {
        escrow_id: input.escrow_id.clone(),
        tracking_number: tracking.clone(),
        provider_id: provider_id.clone(),
        reported_status: String::new(),
        verified: false,
        reason,
    };

    if input.escrow_id.is_empty() {
        print_output(fail("missing escrow_id".into()))?;
        return Ok(());
    }
    if tracking.is_empty() {
        print_output(fail("carrier payload has no tracking_number".into()))?;
        return Ok(());
    }

    // 1. Tracking number must match the registered shipment.
    if let Some(expected_tracking) = &input.expected.tracking_number {
        if expected_tracking != &tracking {
            print_output(fail(format!(
                "tracking mismatch: registered {expected_tracking}, carrier reported {tracking}"
            )))?;
            return Ok(());
        }
    } else {
        print_output(fail("escrow has no registered tracking number".into()))?;
        return Ok(());
    }

    // 2. Provider identity must be present when the escrow records one.
    if input.expected.provider_wallet.is_some() && provider_id.is_empty() {
        print_output(fail("carrier payload has no provider_id".into()))?;
        return Ok(());
    }

    // 3. Status must normalize into the supported set.
    let Some(status) = normalize_status(raw_status) else {
        print_output(fail(format!("unsupported carrier status: {raw_status:?}")))?;
        return Ok(());
    };

    // 4. Event timeline consistency (when events are supplied): happy-chain
    //    statuses must be non-decreasing; a failure status may only be last.
    let events = carrier
        .get("events")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let mut last_rank: Option<usize> = None;
    let mut seen_pickup = false;
    for (idx, event) in events.iter().enumerate() {
        let ev_status_raw = event.get("status").and_then(|v| v.as_str()).unwrap_or("");
        let Some(ev_status) = normalize_status(ev_status_raw) else {
            print_output(fail(format!("event {idx}: unsupported status {ev_status_raw:?}")))?;
            return Ok(());
        };
        if FAILURE_STATUSES.contains(&ev_status.as_str()) {
            if idx + 1 != events.len() {
                print_output(fail(format!("event {idx}: failure status {ev_status} is not the last event")))?;
                return Ok(());
            }
            continue;
        }
        let ev_rank = rank(&ev_status).expect("happy status ranked");
        if ev_status == "PICKED_UP" {
            seen_pickup = true;
        }
        if let Some(prev) = last_rank {
            if ev_rank < prev {
                print_output(fail(format!(
                    "event timeline goes backwards at event {idx}: {ev_status} after rank {prev}"
                )))?;
                return Ok(());
            }
        }
        last_rank = Some(ev_rank);
    }
    if let Some(last) = last_rank {
        let status_rank = rank(&status).unwrap_or(last);
        if status_rank < last {
            print_output(fail(format!(
                "reported status {status} is behind the recorded timeline"
            )))?;
            return Ok(());
        }
    }

    // 5. A DELIVERED report without any pickup evidence is implausible.
    if status == "DELIVERED" && !events.is_empty() && !seen_pickup {
        print_output(fail("DELIVERED reported but the timeline has no PICKED_UP event".into()))?;
        return Ok(());
    }

    print_output(Output {
        escrow_id: input.escrow_id,
        tracking_number: tracking,
        provider_id,
        reported_status: status,
        verified: true,
        reason: "carrier report is internally consistent with the registered shipment".into(),
    })?;
    Ok(())
}

fn print_output(output: Output) -> Result<(), Box<dyn std::error::Error>> {
    let json = serde_json::to_string(&output)?;
    print!("{json}");
    io::stdout().flush()?;
    Ok(())
}
