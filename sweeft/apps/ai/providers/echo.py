"""Deterministic offline provider — no network, no keys.

Returns a machine-readable marker the services understand; combined with their
rule-based fallbacks this keeps every AI feature functional without any
provider configured.
"""
from __future__ import annotations

import json

from .base import AIProviderError, Completion


class EchoProvider:
    name = "echo"
    model = "deterministic-rules"

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> Completion:
        # The services treat this marker as "use your deterministic rules".
        payload = {"provider": "echo", "note": "deterministic offline provider", "input": user[:400]}
        return Completion(text=json.dumps(payload), provider=self.name, model=self.model)
