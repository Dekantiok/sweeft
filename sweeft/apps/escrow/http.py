"""Shared helpers for JSON API endpoints."""
from __future__ import annotations

import json


def read_json(request):
    """Parse a JSON object from the request body.

    Returns the parsed dict, or ``None`` when the body is missing, malformed,
    not UTF-8, or not a JSON object. Callers must answer 400 on ``None``.
    """
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
