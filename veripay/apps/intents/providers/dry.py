"""Dry-run Intents provider — offline, deterministic, clearly simulated.

Used by default so the UI and APIs work end-to-end before the real 1Click
integration is reviewed and enabled.
"""
from __future__ import annotations

import hashlib

from .base import Quote

RATES = {
    ("eth:usdc", "nep141:tusd"): 1.0,
    ("eth:usdt", "nep141:tusd"): 1.0,
    ("sol:usdc", "nep141:tusd"): 1.0,
    ("eth:usdc", "near"): 0.0033,  # 1 USDC buys ~0.0033 NEAR at demo rate
}


class DryIntentsProvider:
    name = "dry"
    dry_run = True

    def quote(self, *, asset_out: str, amount_out: str, asset_in: str = "eth:usdc") -> Quote:
        rate = RATES.get((asset_in, asset_out), 1.0)
        try:
            amount_in = f"{float(amount_out) * rate:.6f}".rstrip("0").rstrip(".")
        except ValueError:
            amount_in = amount_out
        digest = hashlib.sha256(f"{asset_in}:{asset_out}:{amount_out}".encode()).hexdigest()[:10]
        return Quote(
            quote_id=f"dry-{digest}",
            asset_in=asset_in,
            amount_in=amount_in,
            asset_out=asset_out,
            amount_out=amount_out,
            deposit_address=f"dry-deposit-{digest}.simulated",
            provider=self.name,
            dry_run=True,
            raw={
                "simulated": True,
                "note": "SIMULATED QUOTE — dry-run provider. Enable INTENTS_PROVIDER=oneclick for real quotes.",
            },
        )

    def status(self, quote_id: str) -> dict:
        return {
            "quote_id": quote_id,
            "state": "SIMULATED_PENDING",
            "simulated": True,
            "note": "Dry-run provider — no cross-chain transfer can actually happen.",
        }
