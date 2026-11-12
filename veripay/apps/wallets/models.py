from django.db import models


class WalletIdentity(models.Model):
    """A NEAR wallet that has authenticated via a signed NEP-413 challenge."""

    account_id = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=120, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.account_id


class WalletChallenge(models.Model):
    """Single-use, expiring challenge nonce (NEP-413)."""

    nonce = models.CharField(max_length=64, unique=True)
    account_id = models.CharField(max_length=64, blank=True)
    message = models.TextField()
    recipient = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed = models.BooleanField(default=False)
    used_by = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return f"challenge {self.nonce[:12]}… for {self.account_id or 'any'}"
