# OutLayer delivery verification

## What it proves — and what it does not

OutLayer runs our published WebAssembly module inside an Intel TDX enclave and produces a
cryptographic attestation binding **the exact code hash, the exact input, and the output** to a
verified worker. The contract records the returned result only through its own callback.

It does **not** prove the physical contents or condition of a parcel, and it does not replace the
carrier as the source of tracking information.

## The published module

`wasi/sweeft-verify` — Rust, compiled to `wasm32-wasip2`, published on OutLayer testnet as the
project **`steepdata3968.testnet/sweeft-verify`** (hash-pinned; the worker refuses bytes that do
not match the recorded SHA-256).

Input (stdin JSON, built by the contract):

```json
{
  "escrow_id": "SWFT-611104",
  "expected": { "tracking_number": "DEMO-…", "provider_wallet": "logistics.….testnet",
                 "shipment_id": "SHIP-SWFT-611104" },
  "carrier":  { "provider_id": "LOG-DEMO-001", "tracking_number": "DEMO-…",
                 "status": "DELIVERED", "events": [ {"status": "PICKED_UP"}, … ] }
}
```

Output (stdout, exactly one JSON value; the contract deserializes it):

```json
{ "escrow_id": "SWFT-611104", "tracking_number": "DEMO-…", "provider_id": "LOG-DEMO-001",
  "reported_status": "DELIVERED", "verified": true, "reason": "carrier report is internally consistent…" }
```

Checks performed inside the TEE:

1. tracking number matches the registered shipment;
2. provider identity is present;
3. status normalizes into the supported set
   (`CREATED, ACCEPTED, PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, DELIVERY_FAILED,
   RETURNED, LOST, CANCELLED`);
4. the event timeline is monotonic on the happy chain; a failure status may only be last;
5. `DELIVERED` requires pickup evidence in the timeline when events are supplied.

## On-chain integration

1. `request_delivery_verification(escrow_id, carrier_payload)` — **permissionless**: the state
   machine (`Shipped`, no pending verification) and the attached compute deposit gate it. The
   platform relayer fires it automatically when the carrier reports delivery; any party can fire it
   manually as a fallback. The contract marks `verification_pending` and calls
   `outlayer.testnet::request_execution` (`source = Project{…}`, `response_format = Json`).
2. OutLayer executes the module in the TEE and returns the result via the promise callback.
3. `on_verification_result` ( `#[private]` — only reachable from the contract's own promise chain )
   validates the escrow id, then records `pickup_verified`, `delivered_verified`, state
   `AwaitingBuyerConfirmation`, and starts the inspection clock (`delivery_time_ms`).
4. Settlement is permissionless too — destinations and amounts are fixed by the escrow, so the
   relayer (or anyone) can call `settle_logistics` once pickup is verified, and
   `release_after_inspection` once the inspection window elapses with no problem reported. The
   buyer's `confirm_receipt` remains the explicit early-release path; a reported problem blocks the
   timeout release and routes the escrow to review.

No ordinary Django request can record a verification: the only method that writes the flags is
callable exclusively by the contract itself. Testnet run ids are visible in
`outlayer logs` / the OutLayer dashboard under the published project.

## Carrier payload source (MVP honesty)

The Demo Carrier payload is passed as the module **input** (from Django, which stores the
simulated tracking events); the attestation therefore proves the validation logic ran on exactly
that input. Fetching carrier APIs from inside the TEE (WASI P2 HTTP) is the natural next step for
real carriers; it is documented as future work rather than claimed today.
