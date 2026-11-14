//! Sweeft delivery verification module — runs inside OutLayer's Intel TDX TEE.
//!
//! Input (stdin, JSON):
//! {
//!   "escrow_id": "SWFT-611104",
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

use ed25519_dalek::{Signature, Verifier, VerifyingKey};
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
    /// Carrier signing key (hex ed25519 pubkey) configured on-chain. When set,
    /// the carrier payload MUST carry a valid `signature` over the canonical
    /// message below — the caller cannot forge carrier data anymore.
    #[serde(default)]
    carrier_pubkey: Option<String>,
}

/// Canonical carrier message (v1). Must match the signer byte-for-byte:
/// `v1|provider_id|tracking_number|shipment_id|status|issued_at|<event statuses, comma-joined>`
/// Statuses are the RAW strings from the payload, before normalization.
fn canonical_carrier_message(
    provider_id: &str,
    tracking: &str,
    shipment_id: &str,
    status_raw: &str,
    issued_at: u64,
    event_statuses: &[String],
) -> String {
    format!(
        "v1|{}|{}|{}|{}|{}|{}",
        provider_id,
        tracking,
        shipment_id,
        status_raw,
        issued_at,
        event_statuses.join(",")
    )
}

fn decode_hex(value: &str) -> Option<Vec<u8>> {
    if value.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(value.len() / 2);
    let bytes = value.as_bytes();
    for i in (0..bytes.len()).step_by(2) {
        let hi = (bytes[i] as char).to_digit(16)?;
        let lo = (bytes[i + 1] as char).to_digit(16)?;
        out.push(((hi << 4) | lo) as u8);
    }
    Some(out)
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
const CARRIER_PAYLOAD_MAX_AGE_MS: u64 = 60 * 60 * 1_000; // 1 hour
const CARRIER_PAYLOAD_FUTURE_SKEW_MS: u64 = 5 * 60 * 1_000; // 5 minutes

fn normalize_status(raw: &str) -> Option<String> {
    let key = raw.trim().to_ascii_uppercase().replace([' ', '-'], "_");
    ALL_STATUSES
        .iter()
        .find(|s| **s == key)
        .map(|s| s.to_string())
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
    let carrier_shipment = carrier
        .get("shipment_id")
        .and_then(|v| v.as_str())
        .unwrap_or_default();
    match &input.expected.shipment_id {
        Some(expected_shipment) if carrier_shipment == expected_shipment => {}
        Some(expected_shipment) => {
            return fail(format!(
                "shipment mismatch: on-chain {expected_shipment}, carrier reported {carrier_shipment:?}"
            ))
        }
        None => return fail("escrow has no shipment_id".into()),
    }

    // 2c. Carrier authenticity — MANDATORY. The on-chain config must carry a
    //     carrier signing key and the payload must be signed by it over the
    //     canonical message. Unsigned payloads never pass, so a caller cannot
    //     fabricate carrier data from public ids alone.
    let Some(pubkey_hex) = &input.expected.carrier_pubkey else {
        return fail("carrier pubkey not configured on-chain".into());
    };
    let sig_hex = carrier
        .get("signature")
        .and_then(|v| v.as_str())
        .unwrap_or_default();
    if sig_hex.is_empty() {
        return fail("carrier signature required but missing".into());
    }
    let Some(pubkey_bytes) = decode_hex(pubkey_hex) else {
        return fail("malformed carrier pubkey config".into());
    };
    let Ok(pubkey_arr) = <[u8; 32]>::try_from(pubkey_bytes.as_slice()) else {
        return fail("carrier pubkey must be 32 bytes".into());
    };
    let Ok(verifying) = VerifyingKey::from_bytes(&pubkey_arr) else {
        return fail("invalid carrier pubkey".into());
    };
    let Some(sig_bytes) = decode_hex(sig_hex) else {
        return fail("carrier signature must be hex".into());
    };
    let Ok(sig_arr) = <[u8; 64]>::try_from(sig_bytes.as_slice()) else {
        return fail("carrier signature must be 64 bytes".into());
    };
    let signature = Signature::from_bytes(&sig_arr);
    let Some(issued_at) = carrier.get("issued_at").and_then(|v| v.as_u64()) else {
        return fail("carrier issued_at required".into());
    };
    let raw_events: Vec<String> = carrier
        .get("events")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|e| {
            e.get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string()
        })
        .collect();
    let message = canonical_carrier_message(
        &provider_id,
        &tracking,
        carrier_shipment,
        raw_status,
        issued_at,
        &raw_events,
    );
    if verifying.verify(message.as_bytes(), &signature).is_err() {
        return fail("carrier signature invalid".into());
    }

    // Replay protection: a correctly signed carrier report may only be used
    // for a short window after the carrier issued it.
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    if issued_at > now_ms + CARRIER_PAYLOAD_FUTURE_SKEW_MS {
        return fail("carrier payload issued in the future".into());
    }
    if now_ms > issued_at + CARRIER_PAYLOAD_MAX_AGE_MS {
        return fail("carrier payload is stale".into());
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
                return fail(format!(
                    "event {idx}: failure status {ev_status} is not the last event"
                ));
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
            return fail(format!(
                "reported status {status} is behind the recorded timeline"
            ));
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

    fn signer() -> (ed25519_dalek::SigningKey, String) {
        let sk = ed25519_dalek::SigningKey::from_bytes(&[7u8; 32]);
        let pubkey_hex: String = sk
            .verifying_key()
            .to_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        (sk, pubkey_hex)
    }

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    /// Fully signed carrier payload (the signature is MANDATORY).
    fn input(
        status: &str,
        events: serde_json::Value,
        provider_id: &str,
        shipment_id: &str,
    ) -> String {
        signed_input(status, events, provider_id, shipment_id, now_ms())
    }

    fn signed_input(
        status: &str,
        events: serde_json::Value,
        provider_id: &str,
        shipment_id: &str,
        issued_at: u64,
    ) -> String {
        use ed25519_dalek::Signer;
        let (sk, pubkey_hex) = signer();
        let event_statuses: Vec<String> = events
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|e| e["status"].as_str().unwrap_or("").to_string())
                    .collect()
            })
            .unwrap_or_default();
        let message = canonical_carrier_message(
            provider_id,
            "DEMO-1",
            shipment_id,
            status,
            issued_at,
            &event_statuses,
        );
        let signature = sk.sign(message.as_bytes());
        serde_json::json!({
            "escrow_id": "SWFT-611104",
            "expected": {
                "tracking_number": "DEMO-1",
                "provider_wallet": "log.testnet",
                "shipment_id": "SHIP-1",
                "provider_id": "LOG-DEMO-001",
                "carrier_pubkey": pubkey_hex
            },
            "carrier": {
                "provider_id": provider_id,
                "tracking_number": "DEMO-1",
                "shipment_id": shipment_id,
                "status": status,
                "issued_at": issued_at,
                "events": events,
                "signature": hex(&signature.to_bytes())
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
    fn carrier_signature_valid_verifies() {
        let out = run(&happy_input());
        assert!(out.verified, "reason: {}", out.reason);
    }

    #[test]
    fn stale_signed_payload_rejected() {
        let stale = signed_input(
            "DELIVERED",
            serde_json::json!([{"status":"PICKED_UP"},{"status":"DELIVERED"}]),
            "LOG-DEMO-001",
            "SHIP-1",
            now_ms() - CARRIER_PAYLOAD_MAX_AGE_MS - 1,
        );
        let out = run(&stale);
        assert!(!out.verified);
        assert!(out.reason.contains("stale"), "reason: {}", out.reason);
    }

    #[test]
    fn missing_issued_at_rejected() {
        let mut v: serde_json::Value = serde_json::from_str(&happy_input()).unwrap();
        v["carrier"].as_object_mut().unwrap().remove("issued_at");
        let out = run(&v.to_string());
        assert!(!out.verified);
        assert!(
            out.reason.contains("issued_at required"),
            "reason: {}",
            out.reason
        );
    }

    #[test]
    fn tampered_events_after_signing_rejected() {
        // Swap an event status after signing: identity checks still pass but
        // the signature must fail.
        let mut v: serde_json::Value = serde_json::from_str(&happy_input()).unwrap();
        v["carrier"]["events"][0]["status"] = serde_json::json!("ACCEPTED");
        let out = run(&v.to_string());
        assert!(!out.verified);
        assert!(out.reason.contains("signature"), "reason: {}", out.reason);
    }

    #[test]
    fn tampered_payload_after_signing_rejected() {
        let mut v: serde_json::Value = serde_json::from_str(&happy_input()).unwrap();
        v["carrier"]["status"] = serde_json::json!("DELIVERY_FAILED");
        let out = run(&v.to_string());
        assert!(!out.verified);
        assert!(out.reason.contains("signature"), "reason: {}", out.reason);
    }

    #[test]
    fn missing_signature_with_configured_key_rejected() {
        let mut v: serde_json::Value = serde_json::from_str(&happy_input()).unwrap();
        v["carrier"].as_object_mut().unwrap().remove("signature");
        let out = run(&v.to_string());
        assert!(!out.verified);
        assert!(
            out.reason.contains("signature required"),
            "reason: {}",
            out.reason
        );
    }

    #[test]
    fn missing_carrier_pubkey_rejected() {
        // Mandatory config: a fresh deployment without a carrier key never
        // verifies unsigned payloads.
        let mut v: serde_json::Value = serde_json::from_str(&happy_input()).unwrap();
        v["expected"]
            .as_object_mut()
            .unwrap()
            .remove("carrier_pubkey");
        let out = run(&v.to_string());
        assert!(!out.verified);
        assert!(
            out.reason.contains("carrier pubkey not configured"),
            "reason: {}",
            out.reason
        );
    }

    #[test]
    fn delivered_with_empty_events_rejected() {
        let out = run(&input(
            "DELIVERED",
            serde_json::json!([]),
            "LOG-DEMO-001",
            "SHIP-1",
        ));
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
        let out = run(&input(
            "SOMEWHERE",
            serde_json::json!([{"status":"PICKED_UP"}]),
            "LOG-DEMO-001",
            "SHIP-1",
        ));
        assert!(!out.verified);
    }

    #[test]
    fn missing_escrow_id_rejected() {
        let raw = happy_input().replace("SWFT-611104", "");
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
