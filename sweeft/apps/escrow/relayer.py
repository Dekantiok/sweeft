"""Verification relayer — platform-side automation for the happy path.

The escrow contract gates every step and fixes every destination and amount at
creation time, so this relayer can only trigger pre-agreed, condition-gated
movements — it can never redirect funds:

  1. carrier reports DELIVERED   → request TEE verification (permissionless);
  2. the TEE result lands on-chain → sync the outcome into the projection;
  3. pickup is TEE-verified       → pay the logistics allocation (permissionless);
  4. inspection window elapsed    → release seller + platform allocations
     (permissionless), unless a problem was reported.

Scheduling: each pass drives the *fast loop* from non-terminal escrows in the
local projection plus delivered shipments; a full paged chain scan
(`_iter_chain_escrows`) runs as a reconciliation pass every
`RELAYER_RECONCILE_SECONDS` so escrows created directly on-chain are picked up
too — and so nothing is ever silently dropped beyond the first 50 rows.

Every action re-reads the chain first, so the loop is idempotent and safe to
run continuously (``manage.py run_relayer``) or once (``--once``). Each pass
writes a heartbeat row that ``/health/`` reports (stale after
`RELAYER_STALE_AFTER_SECONDS`).

Trust disclosure (testnet MVP): ``RELAYER_PRIVATE_KEY`` lives on the demo
server because this is a testnet build; on mainnet it would run inside a
TEE/KMS. Compromise of the key cannot move funds anywhere not already
committed to by the escrow state machine.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.logistics import carriers
from apps.logistics.models import Shipment, VerificationRequest
from apps.logistics.services import sync_verification_from_chain

from . import near
from .models import EscrowAgreement, RelayerHeartbeat
from .server_tx import call_function_as

RELAYER_GAS = 150_000_000_000_000  # 150 Tgas: pays for the OutLayer call + callback
TERMINAL_STATES = ("Completed", "Refunded", "Cancelled", "Rejected")
FAST_LOOP_AGREEMENTS = 100  # per pass; reconciliation covers the rest


def relayer_configured() -> bool:
    return bool(
        getattr(settings, "RELAYER_ENABLED", False)
        and getattr(settings, "RELAYER_ACCOUNT", "")
        and getattr(settings, "RELAYER_PRIVATE_KEY", "")
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _send(method: str, args: dict, deposit: int = 0) -> dict:
    """Sign one escrow-contract call. Non-payable methods require exactly 0
    deposit; only `request_delivery_verification` is given a compute deposit."""
    return call_function_as(
        signer=settings.RELAYER_ACCOUNT,
        private_key=settings.RELAYER_PRIVATE_KEY,
        method_name=method,
        args=args,
        receiver_id=settings.SWEEFT_CONTRACT,
        gas=RELAYER_GAS,
        deposit=deposit,
    )


def _iter_chain_escrows(page: int = 50, start: int = 1):
    """Yield escrows from creation index `start` (1-based), paging the chain view.

    The escrow count bounds the walk, so this scales with the book instead of
    silently stopping at the first page.
    """
    total = int(near.view_call(settings.SWEEFT_CONTRACT, "escrow_count", {}) or 0)
    index = max(1, int(start))
    while index <= total:
        chunk = near.view_call(
            settings.SWEEFT_CONTRACT, "escrows_range", {"start": index, "limit": page}
        ) or []
        if not chunk:
            break
        yield from chunk
        index += page


def _reconcile_due() -> bool:
    """True when the full chain scan is due (or has never run)."""
    row = RelayerHeartbeat.objects.filter(pk=1).first()
    if row is None or row.last_reconcile_at is None:
        return True
    age = (timezone.now() - row.last_reconcile_at).total_seconds()
    return age >= float(getattr(settings, "RELAYER_RECONCILE_SECONDS", 300))


@dataclass
class RelayerReport:
    requested: list[str] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    logistics_paid: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    reconciled: int = 0
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out: list[str] = []
        for label, ids in (
            ("requested verification", self.requested),
            ("synced result", self.synced),
            ("paid logistics", self.logistics_paid),
            ("released after inspection", self.released),
        ):
            if ids:
                out.append(f"{label}: {', '.join(ids)}")
        if self.reconciled:
            out.append(f"chain reconciliation: {self.reconciled} escrows scanned")
        out.extend(f"error — {message}" for message in self.errors)
        return out or ["nothing to do"]


def relayer_tick() -> RelayerReport:
    """One pass: fast loop over local non-terminal escrows, then (every few
    minutes) a full paged chain reconciliation."""
    report = RelayerReport()
    if not relayer_configured():
        report.errors.append("relayer is not configured")
        return report

    try:
        _fast_tick(report)
    except Exception as e:  # noqa: BLE001 - reconciliation still runs
        report.errors.append(f"fast pass: {e}")

    reconciled = False
    try:
        if _reconcile_due():
            _reconcile_chain(report)
            reconciled = True
    except Exception as e:  # noqa: BLE001 - heartbeat still written
        report.errors.append(f"chain reconciliation: {e}")

    try:
        RelayerHeartbeat.record(reconcile=reconciled, error="; ".join(report.errors))
    except Exception as e:  # noqa: BLE001 - never fail the tick on telemetry
        report.errors.append(f"heartbeat write failed: {e}")
    return report


def _fast_tick(report: RelayerReport) -> None:
    """Cheap pass driven by the local projection + delivered shipments."""
    seen: set[str] = set()
    agreements = EscrowAgreement.objects.exclude(state__in=TERMINAL_STATES).order_by(
        "-last_synced", "-created_at"
    )[:FAST_LOOP_AGREEMENTS]
    for agreement in agreements:
        escrow_id = agreement.escrow_id
        seen.add(escrow_id)
        try:
            chain = near.get_escrow_cached(escrow_id)
        except Exception as e:  # noqa: BLE001 — one bad escrow must not stop the loop
            report.errors.append(f"{escrow_id}: {e}")
            continue
        if chain:
            _advance_chain_escrow(chain, report)

    # Carrier reports may reference escrows that predate the projection (or
    # arrive through the standalone carrier service); never cap this queue.
    shipments = (
        Shipment.objects.filter(status="DELIVERED")
        .select_related("provider")
        .order_by("-updated_at")
    )
    for shipment in shipments.iterator():
        if shipment.escrow_id in seen:
            continue  # several shipments can reference one escrow — act once
        seen.add(shipment.escrow_id)
        try:
            _advance_one(shipment, report)
        except Exception as e:  # noqa: BLE001 — one bad escrow must not stop the loop
            report.errors.append(f"{shipment.escrow_id}: {e}")


def _reconcile_chain(report: RelayerReport) -> None:
    """Full paged chain scan: catches escrows the projection doesn't know.

    Starts at the stored high-water mark (the lowest creation index that was
    still non-terminal at the last scan) instead of index 1, and advances it
    after every pass. New escrows land past the end and are covered by the next
    scan. Known edge: a completion whose payout promise fails rolls back to a
    non-terminal state at the same index — the fast loop re-reads app-created
    escrows every tick, and chain-only escrows are reachable through the escrow
    page and the permissionless retry methods.
    """
    row = RelayerHeartbeat.objects.filter(pk=1).first()
    start = max(1, int(row.reconcile_start_index if row else 1))

    seen: set[str] = set()
    lowest_active: int | None = None
    total = start - 1
    for index, chain in enumerate(_iter_chain_escrows(50, start), start=start):
        total = index
        escrow_id = chain.get("id")
        if not escrow_id or escrow_id in seen:
            continue
        seen.add(escrow_id)
        if lowest_active is None and chain.get("state") not in TERMINAL_STATES:
            lowest_active = index
        _advance_chain_escrow(chain, report)
    report.reconciled = len(seen)

    # Persist the new mark: the lowest non-terminal index, or one past the end
    # when the whole scanned book is terminal.
    new_start = lowest_active if lowest_active is not None else total + 1
    RelayerHeartbeat.objects.update_or_create(
        pk=1, defaults={"reconcile_start_index": new_start}
    )


def _advance_chain_escrow(chain: dict, report: RelayerReport) -> None:
    escrow_id = chain.get("id")
    if not escrow_id:
        return
    state = chain.get("state")
    shipment = Shipment.objects.filter(escrow_id=escrow_id).order_by("-created_at").first()

    if shipment and state == "Shipped" and not chain.get("verification_pending") and not chain.get("delivered_verified"):
        _advance_one(shipment, report)
        return

    if chain.get("last_verification"):
        pending = VerificationRequest.objects.filter(escrow_id=escrow_id, status="PENDING").first()
        if pending:
            sync_verification_from_chain(escrow_id, chain=chain)
            report.synced.append(escrow_id)

    if (
        chain.get("pickup_verified")
        and not chain.get("logistics_paid")
        and state in ("Shipped", "AwaitingBuyerConfirmation")
    ):
        result = _send("settle_logistics", {"escrow_id": escrow_id})
        if result["success"]:
            report.logistics_paid.append(escrow_id)
        else:
            report.errors.append(f"{escrow_id}: logistics settle failed: {result['error']}")

    if (
        state == "AwaitingBuyerConfirmation"
        and chain.get("delivered_verified")
        and not chain.get("seller_paid")
        and not chain.get("problem_reported")
    ):
        delivered_ms = _delivery_time_ms(escrow_id)
        if delivered_ms and _now_ms() >= delivered_ms + int(chain.get("inspection_period_ms") or 0):
            result = _send("release_after_inspection", {"escrow_id": escrow_id})
            if result["success"]:
                report.released.append(escrow_id)
            else:
                report.errors.append(f"{escrow_id}: release failed: {result['error']}")


def _advance_one(shipment: Shipment, report: RelayerReport) -> None:
    escrow_id = shipment.escrow_id
    chain = near.get_escrow(escrow_id)
    if not chain:
        return
    state = chain.get("state")

    # 1) Ask for TEE verification once the carrier's final scan is DELIVERED.
    if (
        state == "Shipped"
        and not chain.get("verification_pending")
        and not chain.get("delivered_verified")
    ):
        payload = carriers.get_carrier().tracking(shipment.tracking_number)
        # Contract-side identity checks need the on-chain shipment id.
        payload["shipment_id"] = chain.get("shipment_id")
        # Carrier signature for the TEE (public key registered on-chain).
        payload = carriers.sign_carrier_payload(payload)
        if payload.get("status") != "DELIVERED":
            return  # wait for the carrier's final scan
        if not VerificationRequest.objects.filter(escrow_id=escrow_id, status="PENDING").exists():
            VerificationRequest.objects.create(
                escrow_id=escrow_id,
                shipment=shipment,
                status="PENDING",
                requested_by=settings.RELAYER_ACCOUNT,
                input_payload=payload,
                attestation_note=(
                    "OutLayer TEE (Intel TDX) — genuine attestation; carrier data is SIMULATED "
                    "Demo Carrier. Triggered automatically by the platform relayer."
                ),
            )
        result = _send(
            "request_delivery_verification",
            {"escrow_id": escrow_id, "carrier_payload": json.dumps(payload)},
            deposit=settings.RELAYER_VERIFICATION_DEPOSIT_YOCTO,
        )
        if result["success"]:
            report.requested.append(escrow_id)
        else:
            report.errors.append(f"{escrow_id}: verification request failed: {result['error']}")
        return  # the TEE callback completes asynchronously; continue next tick

    # 2) Sync the attested outcome into the projection.
    if chain.get("last_verification"):
        pending = VerificationRequest.objects.filter(escrow_id=escrow_id, status="PENDING").first()
        if pending:
            sync_verification_from_chain(escrow_id)
            report.synced.append(escrow_id)

    # 3) Pay the logistics allocation once pickup is TEE-verified.
    if (
        chain.get("pickup_verified")
        and not chain.get("logistics_paid")
        and state in ("Shipped", "AwaitingBuyerConfirmation")
    ):
        result = _send("settle_logistics", {"escrow_id": escrow_id})
        if result["success"]:
            report.logistics_paid.append(escrow_id)
        else:
            report.errors.append(f"{escrow_id}: logistics settle failed: {result['error']}")

    # 4) Release after the inspection window, unless a problem was reported.
    if (
        state == "AwaitingBuyerConfirmation"
        and chain.get("delivered_verified")
        and not chain.get("seller_paid")
        and not chain.get("problem_reported")
    ):
        delivered_ms = _delivery_time_ms(escrow_id)
        window_ms = int(chain.get("inspection_period_ms") or 0)
        if delivered_ms and _now_ms() >= delivered_ms + window_ms:
            result = _send("release_after_inspection", {"escrow_id": escrow_id})
            if result["success"]:
                report.released.append(escrow_id)
            else:
                report.errors.append(f"{escrow_id}: release failed: {result['error']}")


def _delivery_time_ms(escrow_id: str):
    try:
        return near.view_call(
            settings.SWEEFT_CONTRACT, "delivery_time_ms", {"escrow_id": escrow_id}
        )
    except Exception:  # noqa: BLE001 — pre-upgrade escrows have no clock
        return None
