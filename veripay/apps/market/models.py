from django.db import models
from django.utils.text import slugify


class Listing(models.Model):
    """A marketplace listing (off-chain catalog; funds and identity stay on-chain).

    Trust comes from the seller's on-chain reputation and the wallet-signed
    escrow flow; this table is discovery only and can be re-indexed by anyone
    reading the same chain data.
    """

    KIND_CHOICES = [("goods", "Goods"), ("service", "Service")]
    STATUS_CHOICES = [("active", "Active"), ("paused", "Paused"), ("sold", "Sold")]

    seller_account = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="goods")
    category = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    price_amount = models.CharField(max_length=40, help_text="tUSD units (6 decimals), string")
    location = models.CharField(max_length=120, blank=True)
    photos = models.JSONField(default=list, blank=True, help_text="off-chain image references")
    dispatch_days = models.PositiveIntegerField(default=7)
    inspection_days = models.PositiveIntegerField(default=2)
    logistics_note = models.CharField(max_length=200, blank=True)
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
        from apps.escrow.near import format_token

        return format_token(self.price_amount)


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
