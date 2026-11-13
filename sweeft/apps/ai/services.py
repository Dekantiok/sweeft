"""High-level AI use cases.

Each service asks the configured provider for JSON, validates it, and falls
back to deterministic rules when the provider is echo/unavailable — so the
product never breaks because an AI provider is down or unconfigured.
"""
from __future__ import annotations

import json
import re

from .providers.base import AIProviderError
from .registry import get_provider

ACCOUNT_RE = re.compile(r"\b([a-z0-9._-]{2,64}\.(?:testnet|near))\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(usd|near)?", re.IGNORECASE)
DAYS_RE = re.compile(r"(\d+)\s*(day|days|week|weeks)", re.IGNORECASE)


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model response."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# parse_deal_text: natural language -> escrow/listing fields
# ---------------------------------------------------------------------------

PARSE_SYSTEM = (
    "You extract structured deal terms for a NEAR escrow. Reply with JSON only: "
    '{"product": string, "amount": number|null, "token": "NEAR"|null, '
    '"beneficiary": string|null, "deadlineDays": number|null, "terms": string}. '
    "beneficiary must be a NEAR account id if one appears, else null."
)


def deterministic_parse(text: str) -> dict:
    text = (text or "").strip()
    account = None
    m = ACCOUNT_RE.search(text)
    if m:
        account = m.group(1).lower()
    amount = None
    token = None
    m = AMOUNT_RE.search(text)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            amount = None
        if m.group(2):
            token = m.group(2).upper() if m.group(2).lower() == "near" else m.group(2).lower()
    days = None
    m = DAYS_RE.search(text)
    if m:
        days = int(m.group(1))
        if m.group(2).lower().startswith("week"):
            days *= 7
    cleaned = ACCOUNT_RE.sub("", text)
    cleaned = AMOUNT_RE.sub("", cleaned, count=1)
    terms = " ".join(cleaned.split())[:160] or None
    return {
        "product": None,
        "amount": amount,
        "token": token,
        "beneficiary": account,
        "deadlineDays": days,
        "terms": terms,
    }


def parse_deal_text(text: str) -> dict:
    provider = get_provider()
    if provider.name != "echo":
        try:
            raw = provider.complete(system=PARSE_SYSTEM, user=text, json_mode=True, max_tokens=400)
            data = extract_json(raw.text)
            if isinstance(data, dict):
                parsed = deterministic_parse(text)
                parsed.update({k: v for k, v in data.items() if v not in (None, "")})
                return {**parsed, "source": provider.name}
        except AIProviderError:
            pass
    return {**deterministic_parse(text), "source": "rules"}


# ---------------------------------------------------------------------------
# recommend_dispute_split: advisory only — a human always confirms on-chain
# ---------------------------------------------------------------------------

RECOMMEND_SYSTEM = (
    "You advise on a delivery dispute (advisory only; a human confirms). "
    'Reply with JSON only: {"pct": integer 0-100 to the seller, "reason": string <= 300 chars}. '
    "Prefer evidence; when unclear, choose 50 and say why."
)


def deterministic_recommend(evidence: str, terms: str = "") -> dict:
    return {
        "pct": 50,
        "reason": (
            "Deterministic default: with no provider configured the split is an even 50/50. "
            "Review the evidence and adjust before confirming on-chain."
        ),
    }


def recommend_dispute_split(evidence: str, terms: str = "") -> dict:
    provider = get_provider()
    if provider.name != "echo":
        try:
            user = f"Terms: {terms or '(none)'}\n\nEvidence:\n{evidence}"
            raw = provider.complete(system=RECOMMEND_SYSTEM, user=user, json_mode=True, max_tokens=400)
            data = extract_json(raw.text)
            if isinstance(data, dict) and "pct" in data:
                pct = max(0, min(100, int(data["pct"])))
                return {"pct": pct, "reason": str(data.get("reason", ""))[:300], "source": provider.name}
        except (AIProviderError, ValueError, TypeError):
            pass
    return {**deterministic_recommend(evidence, terms), "source": "rules"}


# ---------------------------------------------------------------------------
# draft_listing: marketplace listing suggestions
# ---------------------------------------------------------------------------

LISTING_SYSTEM = (
    "Draft a marketplace listing for physical goods or a service. Reply with JSON only: "
    '{"title": string <= 80 chars, "category": string, "kind": "goods"|"service", "description": string}.'
)


def deterministic_listing(description: str) -> dict:
    words = (description or "").strip().split()
    title = " ".join(words[:8]) or "New listing"
    lowered = (description or "").lower()
    kind = "service" if any(w in lowered for w in ("service", "delivery", "design", "repair", "install")) else "goods"
    return {"title": title[:80], "category": "", "kind": kind, "description": description or ""}


def draft_listing(description: str) -> dict:
    provider = get_provider()
    if provider.name != "echo":
        try:
            raw = provider.complete(system=LISTING_SYSTEM, user=description, json_mode=True, max_tokens=400)
            data = extract_json(raw.text)
            if isinstance(data, dict) and data.get("title"):
                base = deterministic_listing(description)
                base.update({k: v for k, v in data.items() if v not in (None, "")})
                return {**base, "source": provider.name}
        except AIProviderError:
            pass
    return {**deterministic_listing(description), "source": "rules"}
