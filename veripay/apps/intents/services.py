"""Intents services: quotes sized to an escrow's total."""
from __future__ import annotations

from apps.escrow import near

from .providers.base import IntentsError
from .registry import get_intents_provider


def quote_for_escrow(escrow_id: str, asset_out: str = "nep141:tusd", asset_in: str = "eth:usdc") -> dict:
    """Quote the cross-chain amount needed to fund `escrow_id`.

    The escrow records a `foreign_asset` reference; this resolves the total and
    asks the configured provider how much of the buyer's chain asset covers it.
    """
    chain = near.get_escrow(escrow_id)
    if not chain:
        raise IntentsError("escrow not found on-chain")
    total = chain.get("total_amount")
    if not total:
        raise IntentsError("escrow has no total amount")
    provider = get_intents_provider()
    quote = provider.quote(asset_out=asset_out, amount_out=str(total), asset_in=asset_in)
    return {
        "escrow_id": escrow_id,
        "asset_out": quote.asset_out,
        "amount_out": quote.amount_out,
        "asset_in": quote.asset_in,
        "amount_in": quote.amount_in,
        "deposit_address": quote.deposit_address,
        "quote_id": quote.quote_id,
        "provider": quote.provider,
        "dry_run": quote.dry_run,
    }
