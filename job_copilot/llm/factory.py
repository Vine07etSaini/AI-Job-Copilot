"""Pick the LLM provider from .env: LLM_PROVIDER = auto | gemini | groq | ollama | none."""

from __future__ import annotations

import logging
import os

from job_copilot.config import get_profile, get_settings
from job_copilot.llm.chat import LLM, ChatCompletionsLLM
from job_copilot.llm.gemini import Gemini, gemini_available
from job_copilot.llm.providers import GROQ_URL, groq_available, groq_model, ollama_host, ollama_model
from job_copilot.llm.redact import RedactingLLM, profile_pii

log = logging.getLogger(__name__)

PROVIDERS = ("auto", "gemini", "groq", "ollama", "none")


def provider_name() -> str:
    name = (os.getenv("LLM_PROVIDER") or "auto").strip().lower()
    if name not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER '{name}' is not valid. Use one of: {', '.join(PROVIDERS)}.")
    return name


def _available(name: str) -> bool:
    return {"gemini": gemini_available, "groq": groq_available, "ollama": lambda: bool(ollama_model())}[name]()


def _build(name: str) -> LLM:
    if name == "gemini":
        return Gemini()
    if name == "groq":
        return ChatCompletionsLLM("Groq", GROQ_URL, groq_model(), api_key=os.environ["GROQ_API_KEY"])
    return ChatCompletionsLLM("Ollama", f"{ollama_host()}/v1", ollama_model())


def get_llm() -> LLM | None:
    """The configured model, or None so every agent falls back to its offline heuristics.

    `auto` uses the first configured of Gemini (key), Groq (key), Ollama (OLLAMA_MODEL set). Ollama is never probed
    here, so a missing local server shows up as a normal LLM error at call time, not as a slow start.
    With `redact_pii` on (the default) every provider is wrapped so personal details never leave the machine.
    """
    name = provider_name()
    if name == "none":
        return None
    if name == "auto":
        name = next((p for p in ("gemini", "groq", "ollama") if _available(p)), "none")
        if name == "none":
            return None
    elif not _available(name):
        log.warning("LLM_PROVIDER=%s but it is not configured (see .env.example); using offline heuristics.", name)
        return None
    llm = _build(name)
    return RedactingLLM(llm, lambda: profile_pii(get_profile())) if get_settings().redact_pii else llm
