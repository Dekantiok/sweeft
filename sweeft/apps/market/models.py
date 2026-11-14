from django.db import models
from django.utils.text import slugify


class Listing(models.Model):
    """A marketplace listing (off-chain catalog; funds and identity stay on-chain).

    Trust comes from the seller's on-chain reputation and the wallet-signed
    escrow flow; this table is discovery only and can be re-indexed by anyone
    reading the same chain data.
    """

    KIND_CHOICES = [("goods", "Goods"), ("service", "Service")]
    STATUS_CHOICES = [("active", "Active"), ("paused", "Paused"), ("reserved", "Reserved"), ("sold", "Sold")]

    seller_account = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="goods")
    category = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    price_amount = models.CharField(max_length=40, help_text="native NEAR amount in yoctoNEAR, string")
    location = models.CharField(max_length=120, blank=True)
    photos = models.JSONField(default=list, blank=True, help_text="off-chain image references")
    dispatch_days = models.PositiveIntegerField(default=7)
    inspection_days = models.PositiveIntegerField(default=2)
    logistics_note = models.CharField(max_length=200, blank=True)
    logistics_provider = models.CharField(
        max_length=64, blank=True, help_text="Chosen approved provider (settlement wallet), locked into the escrow at creation"
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="active")
    sold_escrow_id = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.seller_account})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:180] or "listing"
            slug = base
            n = 2
            while Listing.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def price_display(self) -> str:
        from apps.escrow.near import format_near_amount

        return format_near_amount(self.price_amount)


class PurchaseRequest(models.Model):
    """Buyer asks the seller to issue an escrow for a listing.

    The contract fixes the seller as creator: a buyer cannot open an escrow in
    the seller's name. So the buyer requests, and the seller creates the
    agreement (prefilled) with one click — both sides still sign on-chain.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("escrowed", "Escrow created"),
        ("declined", "Declined"),
    ]

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="requests")
    buyer_account = models.CharField(max_length=64, db_index=True)
    message = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    escrow_id = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("listing", "buyer_account", "status")]

    def __str__(self):
        return f"{self.buyer_account} → {self.listing.title} [{self.status}]"


class Offer(models.Model):
    """An off-chain price negotiation between a buyer and a listing's seller.

    The offer records intent only — no funds move here. Status is strict:
    proposed → countered (either side may counter) → accepted | rejected |
    withdrawn. On acceptance the record opens a pending PurchaseRequest so the
    existing seller-created escrow flow picks the deal up at the agreed price.
    """

    STATUS_CHOICES = [
        ("proposed", "Proposed"),
        ("countered", "Countered"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
        ("withdrawn", "Withdrawn"),
    ]
    OPEN_STATUSES = ("proposed", "countered")

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="offers")
    buyer_account = models.CharField(max_length=64, db_index=True)
    seller_account = models.CharField(max_length=64, db_index=True)
    amount = models.CharField(max_length=40, help_text="current native NEAR amount in yoctoNEAR, string")
    max_amount = models.CharField(max_length=40, help_text="buyer's ceiling in yoctoNEAR, string")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="proposed")
    message = models.TextField(blank=True)
    history = models.JSONField(default=list, blank=True, help_text="negotiation events: {side, amount, message, action, at}")
    escrow_id = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Offer #{self.pk} on {self.listing_id} by {self.buyer_account} [{self.status}]"

    @property
    def amount_display(self) -> str:
        from apps.escrow.near import format_near_amount

        return format_near_amount(self.amount)

    @property
    def max_display(self) -> str:
        from apps.escrow.near import format_near_amount

        return format_near_amount(self.max_amount)

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    def append_history(self, side: str, action: str, *, amount: str | None = None, message: str = "") -> None:
        """Append one negotiated event; the history list is the audit trail."""
        from django.utils import timezone

        self.history = list(self.history or []) + [
            {
                "side": side,
                "amount": str(amount if amount is not None else self.amount),
                "message": (message or "")[:1000],
                "action": action,
                "at": timezone.now().isoformat(),
            }
        ]

    def accept(self, side: str) -> PurchaseRequest:
        """Mark accepted and make sure the seller's escrow flow has a request.

        ``side`` is who accepted: "seller" (of a proposed/countered offer) or
        "buyer" (of a seller counteroffer).
        """
        self.status = "accepted"
        self.append_history(side=side, action="accepted")
        agreed = f"Offer #{self.pk} accepted at {self.amount_display} NEAR"
        if self.message:
            agreed = f"{agreed} — {self.message}"
        req, created = PurchaseRequest.objects.get_or_create(
            listing=self.listing,
            buyer_account=self.buyer_account,
            status="pending",
            defaults={"message": agreed[:1000]},
        )
        if not created and f"Offer #{self.pk}" not in (req.message or ""):
            req.message = (((req.message + " · ") if req.message else "") + agreed)[:1000]
            req.save(update_fields=["message"])
        self.save(update_fields=["status", "history", "updated_at"])
        return req
