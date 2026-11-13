from django.db import models


class EscrowAgreement(models.Model):
    """Off-chain metadata + projection of an on-chain escrow (SWFT-XXXXXX).

    The contract is the source of truth for state and money; this row stores
    the descriptions, invitation bookkeeping and a cached chain snapshot.
    """

    STATE_CHOICES = [
        ("PendingAcceptance", "Pending acceptance"),
        ("Approved", "Approved"),
        ("Funded", "Funded"),
        ("ShipmentPending", "Shipment pending"),
        ("Shipped", "Shipped"),
        ("AwaitingBuyerConfirmation", "Awaiting buyer confirmation"),
        ("Completed", "Completed"),
        ("Rejected", "Rejected"),
        ("Cancelled", "Cancelled"),
        ("ReviewRequired", "Review required"),
        ("Refunded", "Refunded"),
    ]

    escrow_id = models.CharField(max_length=32, unique=True, db_index=True)
    seller_account = models.CharField(max_length=64, db_index=True)
    buyer_account = models.CharField(max_length=64, blank=True, db_index=True)
    product_name = models.CharField(max_length=200)
    product_description = models.TextField(blank=True)
    product_desc_hash = models.CharField(max_length=128, blank=True)
    seller_amount = models.CharField(max_length=40)
    logistics_amount = models.CharField(max_length=40)
    platform_fee = models.CharField(max_length=40)
    total_amount = models.CharField(max_length=40)
    token_contract = models.CharField(max_length=64)
    state = models.CharField(max_length=40, choices=STATE_CHOICES, default="PendingAcceptance")
    dispatch_deadline_ms = models.BigIntegerField()
    inspection_period_ms = models.BigIntegerField(default=0)
    terms_hash = models.CharField(max_length=128, blank=True)
    create_tx = models.CharField(max_length=120, blank=True)
    last_synced = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    chain_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.escrow_id} — {self.product_name}"

    @property
    def is_settled(self):
        return self.state in {"Completed", "Refunded", "Cancelled", "Rejected"}


class EscrowInvitation(models.Model):
    """High-entropy locator for a pending agreement (Case B).

    The token is a locator, not authorization: acceptance still requires a
    wallet-signed on-chain transaction. Its sha256 is committed on-chain at
    creation time and must be presented to `accept_escrow`.
    """

    STATUS_CHOICES = [("pending", "Pending"), ("accepted", "Accepted"), ("revoked", "Revoked")]

    token = models.CharField(max_length=120, unique=True)
    agreement = models.ForeignKey(EscrowAgreement, on_delete=models.CASCADE, related_name="invitations")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return f"invitation for {self.agreement.escrow_id} ({self.status})"


class ContractEvent(models.Model):
    """Audit trail of on-chain transactions relevant to an agreement."""

    escrow_id = models.CharField(max_length=32, db_index=True)
    event_type = models.CharField(max_length=60)
    tx_hash = models.CharField(max_length=120)
    payload = models.JSONField(default=dict, blank=True)
    block_height = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class SettlementRecord(models.Model):
    """Projection of payouts observed on-chain."""

    PARTY_CHOICES = [
        ("seller", "Seller"),
        ("logistics", "Logistics provider"),
        ("platform", "Platform fee"),
        ("buyer", "Buyer refund"),
    ]

    escrow_id = models.CharField(max_length=32, db_index=True)
    party = models.CharField(max_length=16, choices=PARTY_CHOICES)
    account_id = models.CharField(max_length=64)
    amount = models.CharField(max_length=40)
    tx_hash = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Dispute(models.Model):
    """Off-chain case file for an on-chain dispute (escrow in ReviewRequired).

    The contract locks the funds; this records the story: who opened it, why,
    evidence from both sides, the AI advisory (labelled with its source), and
    how the platform resolution ended. Nothing here can move funds.
    """

    SIDE_CHOICES = [("buyer", "Buyer"), ("seller", "Seller")]
    STATUS_CHOICES = [("open", "Open"), ("resolved", "Resolved"), ("withdrawn", "Withdrawn")]

    escrow_id = models.CharField(max_length=32, db_index=True)
    opened_by = models.CharField(max_length=64)
    side = models.CharField(max_length=8, choices=SIDE_CHOICES)
    reason = models.TextField()
    reason_hash = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="open")
    ai_recommendation = models.JSONField(default=dict, blank=True)
    resolution_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)
    resolution_tx = models.CharField(max_length=120, blank=True)
    opened_tx = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"dispute {self.escrow_id} by {self.opened_by} [{self.status}]"


class DisputeEvidence(models.Model):
    """A statement filed by one party while the dispute is open."""

    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name="evidence")
    author = models.CharField(max_length=64)
    side = models.CharField(max_length=8, choices=Dispute.SIDE_CHOICES)
    text = models.TextField()
    links = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"evidence by {self.author} on {self.dispute.escrow_id}"
