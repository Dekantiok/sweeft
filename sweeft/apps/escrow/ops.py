"""Platform operations console (`/ops/`).

Owner-only overview of the whole ecosystem: live escrow stats, partner
approvals queue, arbitration cases, platform fee configuration and the
dispute review queue. Reads chain data where possible (the contract is the
source of truth).
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import render

from apps.logistics.models import LogisticsPartner

from . import near
from .models import Dispute, EscrowAgreement


def ops_console(request):
    wallet = request.session.get("wallet")
    if not wallet:
        return render(request, "ops.html", {"wallet_account": None})
    try:
        owner = (near.get_config_cached() or {}).get("owner")
    except Exception:  # noqa: BLE001
        owner = None
    if wallet != owner:
        return render(request, "ops.html", {"wallet_account": wallet, "owner": owner, "not_owner": True})

    stats = {}
    try:
        count = int(near.view_call(settings.SWEEFT_CONTRACT, "escrow_count", {}) or 0)
        escrows = near.view_call(settings.SWEEFT_CONTRACT, "escrows_range", {"start": 1, "limit": 50}) or []
    except Exception:  # noqa: BLE001
        count, escrows = 0, []

    gov = None
    try:
        from . import gov as gov_mod

        gov = gov_mod.summary()
    except Exception:  # noqa: BLE001
        gov = None

    completed = [e for e in escrows if e.get("state") == "Completed"]
    disputed = [e for e in escrows if e.get("state") == "ReviewRequired"]
    # Do not mix denominations: legacy tUSD escrows stay out of native totals.
    native_completed = [e for e in completed if e.get("token") == "near"]
    total_volume = sum(int(e.get("seller_amount") or 0) for e in native_completed)
    total_fees = sum(int(e.get("platform_fee") or 0) for e in native_completed)

    arbitration = []
    for e in disputed:
        try:
            case = near.view_call(settings.SWEEFT_CONTRACT, "arbitration_status", {"escrow_id": e["id"]})
        except Exception:  # noqa: BLE001
            case = None
        arbitration.append({"escrow": e, "case": case})

    stats.update(
        {
            "escrow_count": count,
            "completed_count": len(completed),
            "disputed_count": len(disputed),
            "total_volume_display": near.format_near_amount(total_volume),
            "total_fees_display": near.format_near_amount(total_fees),
            "active_partners": LogisticsPartner.objects.filter(status="ACTIVE").count(),
            "pending_partners": LogisticsPartner.objects.filter(status__in=["PENDING", "UNDER_REVIEW"]).count(),
            "open_disputes": Dispute.objects.filter(status="open").count(),
        }
    )
    return render(
        request,
        "ops.html",
        {
            "wallet_account": wallet,
            "owner": owner,
            "stats": stats,
            "recent_escrows": escrows[:12],
            "arbitration": arbitration,
            "gov": gov,
            "pending_partners_list": LogisticsPartner.objects.filter(status__in=["PENDING", "UNDER_REVIEW"]),
            "open_disputes_list": Dispute.objects.filter(status="open")[:20],
            "fee_bps": (near.get_config_cached() or {}).get("platform_fee_bps", 500),
        },
    )
