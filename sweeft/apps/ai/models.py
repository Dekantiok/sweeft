"""Off-chain projections for AI co-pilot payments.

The on-chain allowance is the source of truth for what the agent may spend;
this table records intents and their execution results for audit and UI.
"""
from __future__ import annotations

from django.db import models


class PaymentIntent(models.Model):
    """One proposed co-pilot payment (owner = the wallet granting the budget)."""

    class Status(models.TextChoices):
        PROPOSED = "proposed", "proposed"
        PROCESSING = "processing", "processing"
        EXECUTED = "executed", "executed"
        FAILED = "failed", "failed"

    wallet = models.CharField(max_length=64, db_index=True)
    to_account = models.CharField(max_length=64)
    amount = models.CharField(max_length=40)  # token units (6 decimals), as string
    memo = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    tx_hash = models.CharField(max_length=90, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Proposals expire so an old approval cannot be replayed later; a stale
    # `processing` claim can be reclaimed after the timeout.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone

        return bool(self.expires_at and timezone.now() > self.expires_at)

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f"#{self.pk} {self.wallet} -> {self.to_account} ({self.status})"
