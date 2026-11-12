"""OpenAI-compatible chat completions.

Works with any endpoint exposing POST {base}/chat/completions
(NEAR AI Cloud, OpenAI, Groq, Together, vLLM, Ollama, LM Studio, …).

Verified against NEAR AI Cloud (https://cloud-api.near.ai/v1).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from django.conf import settings

from .base import AIProviderError, Completion


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or settings.AI_BASE_URL).rstrip("/")
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
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
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
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise AIProviderError(f"unexpected provider response shape: {str(data)[:200]}") from e
        return Completion(text=text or "", provider=self.name, model=self.model)
