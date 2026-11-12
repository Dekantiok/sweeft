"""Provider-neutral AI interface.

Every adapter implements `complete()`; higher-level services (parse, recommend,
draft) build prompts, request JSON, and fall back to deterministic rules when a
provider is unavailable. Swapping providers is an env change, never a code
change: AI_PROVIDER=echo|openai_compat|anthropic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AIProviderError(RuntimeError):
    """Raised by adapters when a completion cannot be produced."""


@dataclass
class Completion:
    text: str
    provider: str
    model: str = ""


class AIProvider(Protocol):
    name: str
    model: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> Completion:
        """Return a completion for the given prompts."""
        ...
