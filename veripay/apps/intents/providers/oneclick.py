"""1Click (NEAR Intents) HTTP client.

Implemented against the documented endpoints
(https://docs.near-intents.org — quote, submit deposit, status). Verify against
the CURRENT docs before enabling in production, and keep the dry provider as
the default until then.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from django.conf import settings

from .base import IntentsError, Quote


class OneClickIntentsProvider:
    name = "oneclick"
    dry_run = False

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or settings.INTENTS_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.INTENTS_API_KEY
        self.timeout = timeout or settings.INTENTS_TIMEOUT_SECONDS

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            raise IntentsError(f"1Click HTTP {e.code}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise IntentsError(f"1Click unreachable: {e}") from e

    def quote(self, *, asset_out: str, amount_out: str, asset_in: str = "eth:usdc") -> Quote:
        data = self._post(
            "/v0/quote",
            {
                "dry": False,
                "swapType": "EXACT_OUTPUT",
                "originAsset": asset_in,
                "destinationAsset": asset_out,
                "amount": amount_out,
            },
        )
        quote = data.get("quote", data)
        return Quote(
            quote_id=str(quote.get("depositAddress") or quote.get("correlationId") or ""),
            asset_in=asset_in,
            amount_in=str(quote.get("amountIn", "")),
            asset_out=asset_out,
            amount_out=str(quote.get("amountOut", amount_out)),
            deposit_address=quote.get("depositAddress"),
            provider=self.name,
            dry_run=False,
            raw=quote,
        )

    def status(self, quote_id: str) -> dict:
        req = urllib.request.Request(f"{self.base_url}/v0/status?depositAddress={quote_id}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            raise IntentsError(f"1Click status failed: {e}") from e
