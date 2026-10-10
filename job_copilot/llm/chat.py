"""Provider-neutral LLM interface plus a client for OpenAI-compatible chat APIs (Groq, Ollama)."""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}
MAX_WAIT = 60.0  # never sleep longer than this on a server-suggested Retry-After
JSON_RULES = ("Reply with a single JSON object that validates against this JSON Schema. "
              "No prose, no markdown fences.\nSchema:\n")

T = TypeVar("T", bound=BaseModel)


def _retry_delay(r: httpx.Response, attempt: int) -> float:
    """Honour the server's Retry-After (Groq sends one on 429); else back off exponentially."""
    try:
        return min(float(r.headers["retry-after"]), MAX_WAIT) + 0.5
    except (KeyError, ValueError):
        return float(2 ** (attempt + 1))


class LLM(Protocol):
    """What the agents need from a model; Gemini, Groq and Ollama all satisfy it."""

    def generate_json(self, prompt: str, schema: type[T], system: str | None = None, temperature: float = 0.1) -> T: ...

    def generate_text(self, prompt: str, system: str | None = None, temperature: float = 0.4) -> str: ...


class ChatCompletionsLLM:
    """Talks to `{base_url}/chat/completions` (Groq's and Ollama's OpenAI-compatible endpoints)."""

    def __init__(self, label: str, base_url: str, model: str, api_key: str = "", timeout: float = 120.0,
                 client: httpx.Client | None = None):
        self.label, self.model = label, model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(timeout=timeout)

    def _chat(self, messages: list[dict], temperature: float, json_mode: bool, retries: int = 6) -> str:
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(retries):
            r = self._client.post(self._url, json=body, headers=self._headers)
            if r.status_code in RETRYABLE and attempt < retries - 1:
                wait = _retry_delay(r, attempt)
                log.warning("%s error %s, retrying in %ss", self.label, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        raise RuntimeError("unreachable")

    def generate_text(self, prompt: str, system: str | None = None, temperature: float = 0.4) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        return self._chat(messages, temperature, json_mode=False)

    def generate_json(self, prompt: str, schema: type[T], system: str | None = None, temperature: float = 0.2) -> T:
        """Ask for JSON, validate it against the Pydantic model, and retry once with the validation error."""
        rules = JSON_RULES + json.dumps(schema.model_json_schema())
        messages = [{"role": "system", "content": f"{system}\n\n{rules}" if system else rules},
                    {"role": "user", "content": prompt}]
        text = self._chat(messages, temperature, json_mode=True)
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            log.warning("%s returned JSON that does not match %s, retrying once: %s", self.label, schema.__name__, e)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That did not validate: {e}\nReturn corrected JSON only."}]
            return schema.model_validate_json(self._chat(messages, temperature, json_mode=True))
