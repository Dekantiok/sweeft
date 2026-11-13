import hashlib
import secrets

from django.db import models


class LogisticsPartner(models.Model):
    """A logistics provider; only ACTIVE partners appear in the directory."""

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("UNDER_REVIEW", "Under review"),
        ("APPROVED", "Approved"),
        ("INTEGRATION_PENDING", "Integration pending"),
        ("ACTIVE", "Active"),
        ("SUSPENDED", "Suspended"),
        ("REJECTED", "Rejected"),
    ]

    company_name = models.CharField(max_length=200)
    registration_number = models.CharField(max_length=80, blank=True)
    email = models.EmailField()
    website = models.URLField(blank=True)
    business_address = models.TextField(blank=True)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    operating_locations = models.CharField(max_length=255, blank=True)
    services = models.TextField(blank=True, help_text="One service per line: name — description — base price")
    verification_notes = models.TextField(blank=True)
    tracking_api_endpoint = models.URLField(blank=True)
    settlement_wallet = models.CharField(max_length=64, help_text="NEAR account that receives logistics payouts")
    pricing_note = models.CharField(max_length=255, blank=True)
    owner_wallet = models.CharField(max_length=64, blank=True, help_text="Wallet that manages this partner in the portal")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default="PENDING")
    review_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company_name"]
        verbose_name = "Logistics partner"

    def __str__(self):
        return f"{self.company_name} [{self.status}]"

    @property
    def is_selectable(self):
        return self.status == "ACTIVE"


class PartnerApiCredential(models.Model):
    """Revocable API credential; only the hash is stored."""

    partner = models.ForeignKey(LogisticsPartner, on_delete=models.CASCADE, related_name="credentials")
    prefix = models.CharField(max_length=12)
    hashed_key = models.CharField(max_length=128)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.partner.company_name} key {self.prefix}…"

    @staticmethod
    def generate(partner: LogisticsPartner):
        raw = f"vp_{secrets.token_urlsafe(32)}"
        prefix = raw[:10]
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        cred = PartnerApiCredential.objects.create(partner=partner, prefix=prefix, hashed_key=hashed)
        return raw, cred

    @staticmethod
    def verify_key(raw_key: str):
        if not raw_key:
            return None
        hashed = hashlib.sha256(raw_key.encode()).hexdigest()
        return (
            PartnerApiCredential.objects.filter(hashed_key=hashed, active=True)
            .select_related("partner")
            .first()
        )


class Shipment(models.Model):
    """A shipment registered against a funded escrow (Demo Carrier in MVP)."""

    STATUS_CHOICES = [
        ("CREATED", "Created"),
        ("ACCEPTED", "Accepted by provider"),
        ("PICKED_UP", "Picked up"),
        ("IN_TRANSIT", "In transit"),
        ("OUT_FOR_DELIVERY", "Out for delivery"),
        ("DELIVERED", "Delivered"),
        ("DELIVERY_FAILED", "Delivery failed"),
        ("RETURNED", "Returned"),
        ("LOST", "Lost"),
        ("CANCELLED", "Cancelled"),
    ]

    SIMULATION_SCENARIOS = [
        ("delivered", "Delivered — happy path (~60s run)"),
        ("failed", "Delivery attempt fails"),
        ("lost", "Lost in transit"),
    ]

    escrow_id = models.CharField(max_length=32, db_index=True)
    provider = models.ForeignKey(LogisticsPartner, on_delete=models.PROTECT, related_name="shipments", null=True, blank=True)
    carrier = models.CharField(max_length=40, default="demo")
    tracking_number = models.CharField(max_length=80, unique=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default="CREATED")
    simulated = models.BooleanField(default=True, help_text="Demo Carrier data — clearly labelled as simulated")
    simulation_scenario = models.CharField(
        max_length=16,
        choices=SIMULATION_SCENARIOS,
        default="delivered",
        help_text="Demo Carrier simulated run outcome (the standalone simulator reads this)",
    )
    registered_by = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tracking_number} ({self.status})"


class ShipmentEvent(models.Model):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="events")
    status = models.CharField(max_length=24, choices=Shipment.STATUS_CHOICES)
    note = models.CharField(max_length=255, blank=True)
    simulated = models.BooleanField(default=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.shipment.tracking_number}: {self.status}"


class VerificationRequest(models.Model):
    """OutLayer TEE verification request/result for a shipment."""

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("VERIFIED", "Verified"),
        ("FAILED", "Failed"),
        ("UNAVAILABLE", "Verification unavailable"),
    ]

    escrow_id = models.CharField(max_length=32, db_index=True)
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="verifications")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="PENDING")
    requested_by = models.CharField(max_length=64, blank=True)
    input_payload = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    tx_hash = models.CharField(max_length=120, blank=True)
    attestation_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"verification of {self.shipment.tracking_number} [{self.status}]"
