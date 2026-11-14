"""Tiny cache-backed, per-client rate limiter (no extra dependencies).

Used to protect unauthenticated endpoints that cause RPC or DB work
(challenge issuance, agreement sync, dispute lists) from trivial abuse.
Counters live in the shared database cache, so all gunicorn workers enforce
the same limits.
"""
from __future__ import annotations

import time

from django.conf import settings
from django.core.cache import cache


def client_ip(request) -> str:
    """Client IP, trusting only the proxy hops we control.

    Proxies append to X-Forwarded-For, so with N trusted hops in front of the
    app the client is the Nth entry from the right — never the first, which a
    client can spoof by sending its own header.
    """
    n = max(1, int(getattr(settings, "TRUSTED_PROXY_COUNT", 1)))
    parts = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    return parts[-n] if len(parts) >= n else request.META.get("REMOTE_ADDR", "unknown")


def allow(key: str, limit: int, window_seconds: int = 60) -> bool:
    """True while fewer than `limit` hits happened for `key` in this window."""
    bucket = f"rl:{key}:{int(time.time() // window_seconds)}"
    try:
        current = cache.incr(bucket)
    except ValueError:
        cache.add(bucket, 1, window_seconds + 5)
        current = 1
    return current <= limit
