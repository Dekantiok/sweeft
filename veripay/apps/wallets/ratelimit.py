"""Tiny cache-backed, per-client rate limiter (no extra dependencies).

Used to protect unauthenticated endpoints that cause RPC or DB work
(challenge issuance, agreement sync) from trivial abuse.
"""
from __future__ import annotations

import time

from django.core.cache import cache


def client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def allow(key: str, limit: int, window_seconds: int = 60) -> bool:
    """True while fewer than `limit` hits happened for `key` in this window."""
    bucket = f"rl:{key}:{int(time.time() // window_seconds)}"
    try:
        current = cache.incr(bucket)
    except ValueError:
        cache.add(bucket, 1, window_seconds + 5)
        current = 1
    return current <= limit
