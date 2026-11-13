//! VeriPay delivery verification module — runs inside OutLayer's Intel TDX TEE.
//!
//! Input (stdin, JSON):
//! {
//!   "escrow_id": "VP-000001",
//!   "expected": { "tracking_number": "...", "provider_wallet": "log.testnet", "shipment_id": "SHIP-..." },
//!   "carrier":  { "provider_id": "LOG-001", "tracking_number": "...", "status": "DELIVERED",
//!                 "events": [ { "status": "PICKED_UP", "timestamp": "..." }, ... ] }
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
    /// Carrier-registry provider id (e.g. LOG-DEMO-001) configured on-chain.
    #[serde(default)]
    provider_id: Option<String>,
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

/// The whole verification, as a pure function of the raw JSON input.
fn run(raw: &str) -> Output {
    let input: Input = match serde_json::from_str(raw) {
        Ok(v) => v,
        Err(e) => {
            return Output {
                escrow_id: String::new(),
                tracking_number: String::new(),
                provider_id: String::new(),
                reported_status: String::new(),
                verified: false,
                reason: format!("malformed input json: {e}"),
            }
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
        return fail("missing escrow_id".into());
    }
    if tracking.is_empty() {
        return fail("carrier payload has no tracking_number".into());
    }

    // 1. Tracking number must match the registered shipment.
    if let Some(expected_tracking) = &input.expected.tracking_number {
        if expected_tracking != &tracking {
            return fail(format!(
                "tracking mismatch: registered {expected_tracking}, carrier reported {tracking}"
            ));
        }
    } else {
        return fail("escrow has no registered tracking number".into());
    }

    // 2. Provider identity: must be present, and must equal the carrier id
    //    configured on-chain when one is set.
    if input.expected.provider_wallet.is_some() && provider_id.is_empty() {
        return fail("carrier payload has no provider_id".into());
    }
    if let Some(expected_provider) = &input.expected.provider_id {
        if &provider_id != expected_provider {
            return fail(format!(
                "provider mismatch: on-chain {expected_provider}, carrier reported {provider_id}"
            ));
        }
    }

    // 2b. Shipment identity must match the on-chain shipment id.
    let carrier_shipment = carrier.get("shipment_id").and_then(|v| v.as_str()).unwrap_or_default();
    match &input.expected.shipment_id {
        Some(expected_shipment) if carrier_shipment == expected_shipment => {}
        Some(expected_shipment) => {
            return fail(format!(
                "shipment mismatch: on-chain {expected_shipment}, carrier reported {carrier_shipment:?}"
            ))
        }
        None => return fail("escrow has no shipment_id".into()),
    }

    // 3. Status must normalize into the supported set.
    let Some(status) = normalize_status(raw_status) else {
        return fail(format!("unsupported carrier status: {raw_status:?}"));
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
            return fail(format!("event {idx}: unsupported status {ev_status_raw:?}"));
        };
        if FAILURE_STATUSES.contains(&ev_status.as_str()) {
            if idx + 1 != events.len() {
                return fail(format!("event {idx}: failure status {ev_status} is not the last event"));
            }
            continue;
        }
        let ev_rank = rank(&ev_status).expect("happy status ranked");
        if ev_status == "PICKED_UP" {
            seen_pickup = true;
        }
        if let Some(prev) = last_rank {
            if ev_rank < prev {
                return fail(format!(
                    "event timeline goes backwards at event {idx}: {ev_status} after rank {prev}"
                ));
            }
        }
        last_rank = Some(ev_rank);
    }
    if let Some(last) = last_rank {
        let status_rank = rank(&status).unwrap_or(last);
        if status_rank < last {
            return fail(format!("reported status {status} is behind the recorded timeline"));
        }
    }

    // 5. A DELIVERED report requires pickup evidence — an empty timeline is
    //    not evidence, so it is rejected too.
    if status == "DELIVERED" && !seen_pickup {
        return fail("DELIVERED reported but the timeline has no PICKED_UP event".into());
    }

    Output {
        escrow_id: input.escrow_id,
        tracking_number: tracking,
        provider_id,
        reported_status: status,
        verified: true,
        reason: "carrier report is internally consistent with the registered shipment".into(),
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = String::new();
    io::stdin().read_to_string(&mut raw)?;
    let output = run(&raw);
    let json = serde_json::to_string(&output)?;
    print!("{json}");
    io::stdout().flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input(status: &str, events: serde_json::Value, provider_id: &str, shipment_id: &str) -> String {
        serde_json::json!({
            "escrow_id": "VP-000003",
            "expected": {
                "tracking_number": "DEMO-1",
                "provider_wallet": "log.testnet",
                "shipment_id": "SHIP-1",
                "provider_id": "LOG-DEMO-001"
            },
            "carrier": {
                "provider_id": provider_id,
                "tracking_number": "DEMO-1",
                "shipment_id": shipment_id,
                "status": status,
                "events": events
            }
        })
        .to_string()
    }

    fn happy_input() -> String {
        input(
            "DELIVERED",
            serde_json::json!([{"status":"PICKED_UP"},{"status":"IN_TRANSIT"},
                               {"status":"OUT_FOR_DELIVERY"},{"status":"DELIVERED"}]),
            "LOG-DEMO-001",
            "SHIP-1",
        )
    }

    #[test]
    fn delivered_with_empty_events_rejected() {
        let out = run(&input("DELIVERED", serde_json::json!([]), "LOG-DEMO-001", "SHIP-1"));
        assert!(!out.verified);
        assert!(out.reason.contains("no PICKED_UP"));
    }

    #[test]
    fn provider_mismatch_rejected() {
        let out = run(&input(
            "DELIVERED",
            serde_json::json!([{"status":"PICKED_UP"},{"status":"DELIVERED"}]),
            "LOG-EVIL-999",
            "SHIP-1",
        ));
        assert!(!out.verified);
        assert!(out.reason.contains("provider mismatch"));
    }

    #[test]
    fn shipment_mismatch_rejected() {
        let out = run(&input(
            "DELIVERED",
            serde_json::json!([{"status":"PICKED_UP"},{"status":"DELIVERED"}]),
            "LOG-DEMO-001",
            "SHIP-WRONG",
        ));
        assert!(!out.verified);
        assert!(out.reason.contains("shipment mismatch"));
    }

    #[test]
    fn happy_path_verifies() {
        let out = run(&happy_input());
        assert!(out.verified);
        assert_eq!(out.reported_status, "DELIVERED");
    }

    #[test]
    fn tracking_mismatch_rejected() {
        // Expected (on-chain) says DEMO-1; the carrier report says CARRIER-9.
        let mut v: serde_json::Value = serde_json::from_str(&input(
            "DELIVERED",
            serde_json::json!([{"status":"PICKED_UP"},{"status":"DELIVERED"}]),
            "LOG-DEMO-001",
            "SHIP-1",
        ))
        .unwrap();
        v["carrier"]["tracking_number"] = serde_json::json!("CARRIER-9");
        let out = run(&v.to_string());
        assert!(!out.verified);
        assert!(out.reason.contains("tracking mismatch"));
    }

    #[test]
    fn backwards_timeline_rejected() {
        let out = run(&input(
            "PICKED_UP",
            serde_json::json!([{"status":"DELIVERED"},{"status":"PICKED_UP"}]),
            "LOG-DEMO-001",
            "SHIP-1",
        ));
        assert!(!out.verified);
        assert!(out.reason.contains("backwards") || out.reason.contains("behind"));
    }

    #[test]
    fn delivered_without_pickup_rejected() {
        let out = run(&input(
            "DELIVERED",
            serde_json::json!([{"status":"ACCEPTED"},{"status":"DELIVERED"}]),
            "LOG-DEMO-001",
            "SHIP-1",
        ));
        assert!(!out.verified);
        assert!(out.reason.contains("no PICKED_UP"));
    }

    #[test]
    fn unknown_status_rejected() {
        let out = run(&input("SOMEWHERE", serde_json::json!([{"status":"PICKED_UP"}]), "LOG-DEMO-001", "SHIP-1"));
        assert!(!out.verified);
    }

    #[test]
    fn missing_escrow_id_rejected() {
        let raw = happy_input().replace("VP-000003", "");
        let out = run(&raw);
        assert!(!out.verified);
        assert!(out.reason.contains("missing escrow_id"));
    }

    #[test]
    fn failure_status_must_be_last() {
        let out = run(&input(
            "PICKED_UP",
            serde_json::json!([{"status":"LOST"},{"status":"PICKED_UP"}]),
            "LOG-DEMO-001",
            "SHIP-1",
        ));
        assert!(!out.verified);
    }
}
