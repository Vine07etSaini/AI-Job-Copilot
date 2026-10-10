"""Thin wrapper around the Google Gen AI SDK (Gemini) using an API key or a Vertex AI service account."""

from __future__ import annotations

import logging
import json
import os
import time
from pathlib import Path
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from job_copilot.config import ROOT

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"
RETRYABLE = {429, 500, 502, 503, 504}

T = TypeVar("T", bound=BaseModel)


class GeminiNotConfigured(RuntimeError):
    pass


def vertex_credentials_path() -> Path | None:
    """The service-account JSON from GOOGLE_APPLICATION_CREDENTIALS (relative paths resolve from the repo root)."""
    if not (raw := os.getenv("GOOGLE_APPLICATION_CREDENTIALS")):
        return None
    path = Path(raw) if Path(raw).is_absolute() else ROOT / raw
    return path if path.is_file() else None


def use_vertex() -> bool:
    """GEMINI_BACKEND = auto | vertex | api_key. auto prefers Vertex when service-account credentials are configured."""
    backend = (os.getenv("GEMINI_BACKEND") or "auto").strip().lower()
    if backend not in ("auto", "vertex", "api_key"):
        raise ValueError(f"GEMINI_BACKEND '{backend}' is not valid. Use auto, vertex or api_key.")
    return backend == "vertex" or (backend == "auto" and vertex_credentials_path() is not None)


def gemini_available() -> bool:
    return vertex_credentials_path() is not None if use_vertex() else bool(os.getenv("GEMINI_API_KEY"))


def model_name() -> str:
    return os.getenv("GEMINI_MODEL") or DEFAULT_MODEL


def _make_vertex_client() -> genai.Client:
    from google.oauth2 import service_account

    if not (path := vertex_credentials_path()):
        raise GeminiNotConfigured("GOOGLE_APPLICATION_CREDENTIALS does not point to a service-account JSON file.")
    creds = service_account.Credentials.from_service_account_file(
        str(path), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or json.loads(path.read_text(encoding="utf-8"))["project_id"]
    return genai.Client(vertexai=True, project=project, location=os.getenv("GOOGLE_CLOUD_LOCATION") or "global",
                        credentials=creds)


def make_client() -> genai.Client:
    if use_vertex():
        return _make_vertex_client()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise GeminiNotConfigured("GEMINI_API_KEY is not set. Add it to your .env file.")
    return genai.Client(api_key=api_key)


class Gemini:
    def __init__(self, model: str | None = None, client: genai.Client | None = None):
        self.client = client or make_client()
        self.model = model or model_name()

    def _generate(self, contents: str, config: types.GenerateContentConfig, retries: int = 4):
        for attempt in range(retries):
            try:
                return self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as e:
                if e.code not in RETRYABLE or attempt == retries - 1:
                    raise
                wait = 2 ** (attempt + 1)
                log.warning("Gemini error %s, retrying in %ss", e.code, wait)
                time.sleep(wait)

    def generate_json(self, prompt: str, schema: type[T], system: str | None = None, temperature: float = 0.2) -> T:
        """Generate a response validated against a Pydantic model."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )
        resp = self._generate(prompt, config)
        if isinstance(resp.parsed, schema):
            return resp.parsed
        return schema.model_validate_json(resp.text)

    def generate_text(self, prompt: str, system: str | None = None, temperature: float = 0.4) -> str:
        config = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
        return self._generate(prompt, config).text or ""
