"""Mask personal details before text goes to an LLM, and put them back in what comes back."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from job_copilot.llm.chat import LLM
from job_copilot.models import Profile

T = TypeVar("T", bound=BaseModel)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]+"
                    r"|\b(?:[\w-]+\.)*(?:github|gitlab|linkedin)\.com/[^\s<>\"'()\[\]]+", re.I)
PHONE_RE = re.compile(r"(?<![\w+])\+?\d[\d\s().-]{8,16}\d(?!\w)")
# On these hosts only the username is personal; keeping the host (and repo name) lets the model tell links apart.
PROFILE_URL_RE = re.compile(r"^((?:https?://)?(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org|linkedin\.com/in)/)"
                            r"([^/?#]+)(.*)$", re.I)
PLACEHOLDER_RE = re.compile(r"\[(?:NAME|EMAIL|PHONE|URL|LOCATION)_\d+\]")
URL_TRAILING = ".,;:!?"
PHONE_DIGITS = range(10, 14)  # fewer digits are years or salary ranges, more are ids
MIN_PART_LEN = 3  # name parts shorter than this ("Al", initials) would mask ordinary words


def profile_pii(profile: Profile) -> dict[str, list[str]]:
    """The personal values in a profile that must never reach an LLM, by kind."""
    p = profile.personal
    return {"name": [p.name], "email": [p.email], "phone": [p.phone], "location": [p.location],
            "url": [p.linkedin, p.github, p.portfolio, *(proj.github for proj in profile.projects)]}


def profile_for_llm(profile: Profile) -> dict:
    """The profile without contact details and links, for tools whose output goes to an LLM."""
    return profile.model_dump(mode="json", exclude={"personal": True, "projects": {"__all__": {"github"}}})


def _words(value: str) -> str:
    return r"\s+".join(re.escape(w) for w in value.split())


def _known_patterns(kind: str, value: str) -> list[tuple[str, re.Pattern]]:
    """Regexes for one known value; names and addresses also match their parts ("Asha" alone, the city alone)."""
    value = value.strip()
    if not value:
        return []
    if kind == "phone":
        digits = re.sub(r"\D", "", value)[-10:]
        sep = r"[\s().-]*"
        return [(value, re.compile(rf"(?<!\d)(?:\+?\d{{1,3}}{sep})?{sep.join(digits)}(?!\d)"))] if digits else []
    if kind == "url":
        core = re.sub(r"^(?:https?://)?(?:www\.)?", "", value, flags=re.I).rstrip("/")
        return [(value, re.compile(rf"(?:https?://)?(?:www\.)?{re.escape(core)}/?(?![\w-])", re.I))]
    if kind == "name":
        parts = [value, *(w for w in value.split() if len(w) >= MIN_PART_LEN)]
    elif kind == "location":  # "Pune, Maharashtra, India": mask it all and every part but the country
        pieces = [s.strip() for s in value.split(",")]
        parts = [value, *(s for s in pieces[:-1] if len(s) >= MIN_PART_LEN)]
    else:
        parts = [value]
    return [(v, re.compile(rf"(?<!\w){_words(v)}(?!\w)", re.I)) for v in dict.fromkeys(parts)]


class Redactor:
    """One masking session: the same value always gets the same placeholder, and the map stays in memory only."""

    def __init__(self, known: dict[str, list[str]]):
        self._tags: dict[tuple[str, str], str] = {}  # (kind, lowercased value) -> placeholder
        self._values: dict[str, str] = {}            # placeholder -> original text
        self._counts: Counter[str] = Counter()
        rules = [(kind, v, rx) for kind, values in known.items() for value in values
                 for v, rx in _known_patterns(kind, value)]
        rules.sort(key=lambda r: -len(r[1]))  # longest first, so a full name wins over its parts
        self._known = {kind: [rx for k, _, rx in rules if k == kind] for kind in known}

    def _tag(self, kind: str, text: str) -> str:
        key = (kind, text.lower())
        if key not in self._tags:
            self._counts[kind] += 1
            tag = f"[{kind.upper()}_{self._counts[kind]}]"
            self._tags[key], self._values[tag] = tag, text
        return self._tags[key]

    def _url(self, m: re.Match) -> str:
        url = m.group(0)
        if m.string.startswith("[", m.end()):  # an already masked link ("https://github.com/[URL_1]")
            return url
        trail = url[len(url.rstrip(URL_TRAILING)):]
        url = url[:len(url) - len(trail)] if trail else url
        if hosted := PROFILE_URL_RE.match(url):
            return hosted.group(1) + self._tag("url", hosted.group(2)) + hosted.group(3) + trail
        return self._tag("url", url) + trail

    def _phone(self, m: re.Match) -> str:
        return self._tag("phone", m.group(0)) if len(re.sub(r"\D", "", m.group(0))) in PHONE_DIGITS else m.group(0)

    def mask(self, text: str) -> str:
        # Emails and links first: a name part inside "asha.rao@..." or "github.com/asha" must not split them.
        steps: list[tuple[re.Pattern, Callable[[re.Match], str]]] = []
        steps += [(rx, lambda m: self._tag("email", m.group(0))) for rx in self._known.get("email", [])]
        steps.append((EMAIL_RE, lambda m: self._tag("email", m.group(0))))
        steps += [(rx, self._url) for rx in self._known.get("url", [])]
        steps.append((URL_RE, self._url))
        steps += [(rx, lambda m: self._tag("phone", m.group(0))) for rx in self._known.get("phone", [])]
        steps.append((PHONE_RE, self._phone))
        for kind in ("name", "location"):
            steps += [(rx, lambda m, k=kind: self._tag(k, m.group(0))) for rx in self._known.get(kind, [])]
        for rx, repl in steps:
            text = rx.sub(repl, text)
        return text

    def unmask(self, text: str) -> str:
        if unknown := sorted({t for t in PLACEHOLDER_RE.findall(text) if t not in self._values}):
            raise ValueError(f"LLM reply has placeholders that were never sent: {', '.join(unknown)}")
        return PLACEHOLDER_RE.sub(lambda m: self._values[m.group(0)], text)


def _walk(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, fn) for k, v in value.items()}
    return value


class RedactingLLM:
    """Wraps any LLM: prompts are masked before they leave the machine and replies are unmasked locally."""

    def __init__(self, inner: LLM, known: Callable[[], dict[str, list[str]]]):
        # `known` is called per request: the profile is not cached and can change at runtime.
        self.inner, self._known = inner, known
        self.label = getattr(inner, "label", type(inner).__name__)

    def also_mask(self, **values: str) -> RedactingLLM:
        """A copy that also masks values the profile doesn't hold yet (the name on a resume being imported)."""
        base = self._known

        def known() -> dict[str, list[str]]:
            pii = base()
            return {**pii, **{kind: [*pii.get(kind, []), v] for kind, v in values.items()}}

        return RedactingLLM(self.inner, known)

    def generate_text(self, prompt: str, system: str | None = None, temperature: float = 0.4) -> str:
        r = Redactor(self._known())
        reply = self.inner.generate_text(r.mask(prompt), system=r.mask(system) if system else None,
                                         temperature=temperature)
        return r.unmask(reply)

    def generate_json(self, prompt: str, schema: type[T], system: str | None = None, temperature: float = 0.2) -> T:
        r = Redactor(self._known())
        draft = self.inner.generate_json(r.mask(prompt), schema, system=r.mask(system) if system else None,
                                         temperature=temperature)
        return schema.model_validate(_walk(draft.model_dump(mode="json"), r.unmask))


def mask_also(llm: LLM, **values: str) -> LLM:
    """Add call-specific values to mask; a no-op when masking is switched off (`redact_pii: false`)."""
    return llm.also_mask(**values) if isinstance(llm, RedactingLLM) else llm
