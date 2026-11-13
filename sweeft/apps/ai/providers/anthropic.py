"""Anthropic Messages API adapter (https://api.anthropic.com/v1/messages)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from django.conf import settings

from .base import AIProviderError, Completion

DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None, timeout: int | None = None):
        base = base_url or (settings.AI_BASE_URL if "anthropic" in settings.AI_BASE_URL else DEFAULT_ANTHROPIC_URL)
        self.base_url = base.rstrip("/")
        self.api_key = api_key if api_key is not None else settings.AI_API_KEY
        self.model = model or settings.AI_MODEL
        self.timeout = timeout or settings.AI_TIMEOUT_SECONDS

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> Completion:
        if not self.api_key:
            raise AIProviderError("AI_API_KEY is not configured")
        instruction = "\n\nRespond with a single valid JSON object and nothing else." if json_mode else ""
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user + instruction}],
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            raise AIProviderError(f"provider HTTP {e.code}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise AIProviderError(f"provider unreachable: {e}") from e

        try:
            text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        except (AttributeError, TypeError) as e:
            raise AIProviderError(f"unexpected provider response shape: {str(data)[:200]}") from e
        return Completion(text=text or "", provider=self.name, model=self.model)
