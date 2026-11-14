"""Template context shared by the marketplace surfaces."""
from __future__ import annotations

from apps.wallets.views import current_wallet

from .models import PurchaseRequest


def seller_request_count(request) -> dict:
    """Pending purchase requests on the signed-in wallet's listings.

    Cheap enough for every page (one indexed COUNT) and rendered as a badge
    next to "My listings" so sellers notice incoming requests.
    """
    wallet = current_wallet(request)
    if not wallet:
        return {"seller_pending_requests": 0}
    count = PurchaseRequest.objects.filter(
        listing__seller_account=wallet, status="pending"
    ).count()
    return {"seller_pending_requests": count}
