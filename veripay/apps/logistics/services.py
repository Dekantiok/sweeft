"""Shared logistics services used by views and the platform relayer."""
from __future__ import annotations

from django.utils import timezone

from apps.escrow import near
from apps.escrow.models import ContractEvent

from .models import VerificationRequest


def sync_verification_from_chain(escrow_id: str, tx_hash: str = "", chain: dict | None = None) -> VerificationRequest | None:
    """Re-read the escrow and update its latest verification record from chain.

    Returns the updated ``VerificationRequest`` (or the existing record when the
    chain has no verification yet, or ``None`` when nothing was ever recorded).
    Recording a verification outcome is idempotent: re-syncing overwrites the
    same fields with the same chain values. Pass ``chain`` when the caller
    already read the escrow to save an RPC round-trip.
    """
    vr = VerificationRequest.objects.filter(escrow_id=escrow_id).order_by("-created_at").first()
    if not vr:
        return None
    if chain is None:
        chain = near.get_escrow(escrow_id)
    last = (chain or {}).get("last_verification")
    if last:
        vr.result = last
        vr.tx_hash = tx_hash
        vr.status = "VERIFIED" if last.get("verified") else "FAILED"
        vr.completed_at = vr.completed_at or timezone.now()
        vr.save(update_fields=["result", "tx_hash", "status", "completed_at"])
        ContractEvent.objects.create(
            escrow_id=escrow_id,
            event_type="verification_recorded",
            tx_hash=vr.tx_hash,
            payload=last,
        )
    return vr
