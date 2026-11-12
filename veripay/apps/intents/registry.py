"""Intents provider registry — INTENTS_PROVIDER selects the adapter."""
from __future__ import annotations

from django.conf import settings

from .providers.base import IntentsError, IntentsProvider
from .providers.dry import DryIntentsProvider

_REGISTRY = {"dry": lambda: DryIntentsProvider()}


def _oneclick():
    from .providers.oneclick import OneClickIntentsProvider

    return OneClickIntentsProvider()


_REGISTRY["oneclick"] = _oneclick


def get_intents_provider() -> IntentsProvider:
    name = (settings.INTENTS_PROVIDER or "dry").strip().lower()
    factory = _REGISTRY.get(name)
    if factory is None:
        raise IntentsError(f"unknown INTENTS_PROVIDER '{name}' (expected one of {sorted(_REGISTRY)})")
    return factory()


def intents_status() -> dict:
    name = (settings.INTENTS_PROVIDER or "dry").strip().lower()
    return {
        "provider": name,
        "dry_run": name == "dry",
        "base_url": settings.INTENTS_BASE_URL if name != "dry" else None,
        "api_key_configured": bool(settings.INTENTS_API_KEY),
        "known_providers": sorted(_REGISTRY.keys()),
        "note": "Cross-chain settlement is wired at the API layer; enable after reviewing 1Click docs.",
    }
