"""Provider-agnostic LLM interface with Gemini (default hosted), OpenAI and Anthropic adapters plus an offline mock.

Only fixed, provider-owned HTTPS endpoints are contacted (no user-controlled URLs, so no SSRF surface). API keys come
only from settings. Any transport/parsing failure raises LLMError; callers treat that as "no model proposal", never
as success. LLM output is always a *proposal* that deterministic code validates.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache

import httpx

from finguard.core.config import get_settings


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    purpose: str            # "extract" | "plan" | "answer"
    max_tokens: int = 2048
    temperature: float = 0.0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    latency_ms: int
    usage: dict = field(default_factory=dict)

    def json(self) -> dict:
        txt = self.text.strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise LLMError("model did not return a JSON object")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError("model returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LLMError("model JSON is not an object")
        return data


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name = "abstract"
    is_mock = False

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    def complete(self, req: LLMRequest) -> LLMResponse: ...


class MockProvider(LLMProvider):
    """Deterministic offline provider. It proposes nothing for extraction (the rule extractor stands alone) and is
    replaced by a deterministic planner in the agent. Labelled as mock everywhere it is used."""

    name = "mock"
    is_mock = True

    def complete(self, req: LLMRequest) -> LLMResponse:
        return LLMResponse(json.dumps({"fields": {}} if req.purpose == "extract" else {}), "mock-deterministic", 0)


class _HttpProvider(LLMProvider):
    def __init__(self, model: str, api_key: str) -> None:
        if not api_key:
            raise LLMError(f"{self.name}: API key not configured")
        super().__init__(model)
        self._key = api_key
        self._timeout = httpx.Timeout(get_settings().llm_timeout_seconds, connect=5.0)

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        last: Exception | None = None
        for attempt in range(4):  # bounded retries: transport error / 5xx / 429 rate limit (honours Retry-After ≤ 20 s)
            try:
                with httpx.Client(timeout=self._timeout, follow_redirects=False) as c:
                    r = c.post(url, headers=headers, json=body)
                if r.status_code == 429:
                    last = LLMError(f"{self.name}: rate limited")
                    try:
                        wait = min(20.0, float(r.headers.get("retry-after", "2")))
                    except ValueError:
                        wait = 2.0
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    last = LLMError(f"{self.name}: HTTP {r.status_code}")
                    continue
                if r.status_code != 200:
                    raise LLMError(f"{self.name}: HTTP {r.status_code}")
                return r.json()
            except httpx.HTTPError as exc:
                last = exc
            time.sleep(0.5 * (attempt + 1))
        raise LLMError(f"{self.name}: request failed ({type(last).__name__})")


class GeminiProvider(_HttpProvider):
    name = "gemini"
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def complete(self, req: LLMRequest) -> LLMResponse:
        t = time.perf_counter()
        data = self._post(self.endpoint.format(model=self.model), {"x-goog-api-key": self._key}, {
            "systemInstruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": [{"text": req.user}]}],
            "generationConfig": {"temperature": req.temperature, "maxOutputTokens": req.max_tokens,
                                 "responseMimeType": "application/json"},
        })
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gemini: unexpected response shape") from exc
        return LLMResponse(text, self.model, int((time.perf_counter() - t) * 1000), data.get("usageMetadata", {}))


class OpenAIProvider(_HttpProvider):
    name = "openai"
    endpoint = "https://api.openai.com/v1/chat/completions"

    def complete(self, req: LLMRequest) -> LLMResponse:
        t = time.perf_counter()
        data = self._post(self.endpoint, {"Authorization": f"Bearer {self._key}"}, {
            "model": self.model, "temperature": req.temperature, "max_tokens": req.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}],
        })
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name}: unexpected response shape") from exc
        return LLMResponse(text, self.model, int((time.perf_counter() - t) * 1000), data.get("usage", {}))


class GroqProvider(OpenAIProvider):
    """Groq (hosted open models, e.g. gpt-oss-120b, Llama) — OpenAI-compatible chat-completions API with JSON mode."""
    name = "groq"
    endpoint = "https://api.groq.com/openai/v1/chat/completions"

    def complete(self, req: LLMRequest) -> LLMResponse:
        t = time.perf_counter()
        body = {"model": self.model, "temperature": req.temperature, "max_tokens": req.max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}]}
        if "gpt-oss" in self.model:
            body["reasoning_effort"] = "low"          # grounded extraction/answering needs little hidden reasoning
        data = self._post(self.endpoint, {"Authorization": f"Bearer {self._key}"}, body)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name}: unexpected response shape") from exc
        return LLMResponse(text, self.model, int((time.perf_counter() - t) * 1000), data.get("usage", {}))


class AnthropicProvider(_HttpProvider):
    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"

    def complete(self, req: LLMRequest) -> LLMResponse:
        t = time.perf_counter()
        data = self._post(self.endpoint, {"x-api-key": self._key, "anthropic-version": "2023-06-01"}, {
            "model": self.model, "max_tokens": req.max_tokens, "temperature": req.temperature,
            "system": req.system, "messages": [{"role": "user", "content": req.user}],
        })
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LLMError("anthropic: unexpected response shape") from exc
        return LLMResponse(text, self.model, int((time.perf_counter() - t) * 1000), data.get("usage", {}))


@lru_cache
def get_provider() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "gemini":
        return GeminiProvider(s.llm_model, s.gemini_api_key)
    if s.llm_provider == "openai":
        return OpenAIProvider(s.llm_model, s.openai_api_key)
    if s.llm_provider == "anthropic":
        return AnthropicProvider(s.llm_model, s.anthropic_api_key)
    if s.llm_provider == "groq":
        return GroqProvider(s.llm_model, s.groq_api_key)
    return MockProvider("mock-deterministic")
