"""Provider registry — AI_PROVIDER selects the adapter, no code changes."""
from __future__ import annotations

from django.conf import settings

from .providers.base import AIProvider, AIProviderError
from .providers.echo import EchoProvider

_REGISTRY = {
    "echo": lambda: EchoProvider(),
}


def _openai_compat():
    from .providers.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider()


def _anthropic():
    from .providers.anthropic import AnthropicProvider

    return AnthropicProvider()


_REGISTRY["openai_compat"] = _openai_compat
_REGISTRY["openai"] = _openai_compat  # alias
_REGISTRY["anthropic"] = _anthropic


def get_provider() -> AIProvider:
    name = (settings.AI_PROVIDER or "echo").strip().lower()
    factory = _REGISTRY.get(name)
    if factory is None:
        raise AIProviderError(f"unknown AI_PROVIDER '{name}' (expected one of {sorted(_REGISTRY)})")
    return factory()


def provider_status() -> dict:
    """Safe, secret-free status for the API and admin."""
    name = (settings.AI_PROVIDER or "echo").strip().lower()
    return {
        "provider": name,
        "model": settings.AI_MODEL if name != "echo" else "deterministic-rules",
        "base_url": settings.AI_BASE_URL if name != "echo" else None,
        "configured": True if name == "echo" else bool(settings.AI_API_KEY),
        "known_providers": sorted(_REGISTRY.keys()),
    }
