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
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f"#{self.pk} {self.wallet} -> {self.to_account} ({self.status})"
