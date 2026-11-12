def wallet_context(request):
    """Expose the connected wallet and its logistics-partner role to templates.

    Partner status is computed from the DB (owner_wallet) so navigation can be
    role-gated: only registered partners see partner-management entry points.
    """
    wallet = request.session.get("wallet")
    partner = None
    if wallet:
        try:
            from apps.logistics.models import LogisticsPartner

            partner = LogisticsPartner.objects.filter(owner_wallet=wallet).first()
        except Exception:  # noqa: BLE001 - nav still renders without partner info
            partner = None
    return {"wallet_account": wallet, "my_partner": partner}
