"""Provider-neutral NEAR Intents interface (cross-chain settlement).

Intents let a buyer pay from another chain (EVM, Solana, …) and settle into
NEAR assets. The escrow contract records a `foreign_asset` reference; this
layer turns that reference into an actual quote + deposit flow.

Providers:
  dry       offline, deterministic, clearly-simulated (default; nothing breaks)
  oneclick  1Click (Intent Swap) HTTP API — implemented against the documented
            endpoints; enable only after reviewing the current docs at
            https://docs.near-intents.org and configuring INTENTS_API_KEY
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class IntentsError(RuntimeError):
    pass


@dataclass
class Quote:
    quote_id: str
    asset_in: str
    amount_in: str
    asset_out: str
    amount_out: str
    deposit_address: str | None = None
    provider: str = "dry"
    dry_run: bool = True
    raw: dict = field(default_factory=dict)


class IntentsProvider(Protocol):
    name: str
    dry_run: bool

    def quote(self, *, asset_out: str, amount_out: str, asset_in: str = "eth:usdc") -> Quote:
        """Quote how much of `asset_in` is needed to receive `amount_out` of `asset_out`."""
        ...

    def status(self, quote_id: str) -> dict:
        """Poll a quote/deposit by reference."""
        ...
