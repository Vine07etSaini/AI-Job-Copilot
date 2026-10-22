"""Verified Skill Bank: canonical names, aliases and approved equivalents."""

from __future__ import annotations

import re

from job_copilot.models import Profile


def norm(term: str) -> str:
    """Normalise a skill term so that 'Node.js', 'NodeJS' and 'node js' compare equal."""
    t = term.lower().strip().replace(".js", "js")
    t = re.sub(r"[^a-z0-9+#]", "", t)
    # crude plural folding ('REST APIs' == 'REST API'); applied to both sides so it stays consistent
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return t


def find_term(text: str, term: str) -> int:
    """Count whole-word, case-insensitive occurrences of `term` in `text`."""
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9+#])"
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


PAREN_RE = re.compile(r"^(.+?)\s*\(([^)]+)\)\s*$")
JS_SUFFIX_RE = re.compile(r"^(\w{3,})\.?js$", re.I)


def name_forms(name: str) -> list[str]:
    """A skill name plus forms JDs use: "Retrieval-Augmented Generation (RAG)" is also "RAG", "React.js" is "React"."""
    if m := PAREN_RE.match(name):
        return [name, m.group(1), m.group(2)]
    if m := JS_SUFFIX_RE.match(name):
        return [name, m.group(1)]
    return [name]


class SkillBank:
    def __init__(self, profile: Profile):
        self.profile = profile
        self._canonical: dict[str, str] = {}
        for s in profile.skills:
            for t in (s.name, *s.aliases):
                self._canonical[norm(t)] = s.name
        for s in profile.skills:  # second pass: a derived form never overrides a name or alias you wrote
            for t in name_forms(s.name)[1:]:
                self._canonical.setdefault(norm(t), s.name)
        self._equivalents: dict[str, list[str]] = {}
        for jd_term, mine in profile.equivalents.items():
            resolved = [self._canonical[norm(m)] for m in mine if norm(m) in self._canonical]
            if resolved:
                self._equivalents[norm(jd_term)] = resolved
        self._learning = {norm(t) for t in profile.learning_goals}

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.profile.skills]

    def canonical(self, term: str) -> str | None:
        """Your canonical skill name for `term`, or None if you don't have it."""
        return self._canonical.get(norm(term))

    def match(self, term: str) -> tuple[str, bool] | None:
        """Return (your skill, via_equivalent) if you can claim `term`, else None."""
        if c := self.canonical(term):
            return c, False
        if eq := self._equivalents.get(norm(term)):
            return eq[0], True
        return None

    def is_learning(self, term: str) -> bool:
        return norm(term) in self._learning

    def skills_in(self, text: str) -> list[str]:
        """Your skills whose name or any alias appears in `text`, in profile order."""
        return [s for s in self.names if any(find_term(text, f) for f in self.surface_forms(s))]

    def surface_forms(self, skill: str) -> list[str]:
        """All spellings of one of your skills (name + aliases)."""
        for s in self.profile.skills:
            if s.name == skill:
                return [*name_forms(s.name), *s.aliases]
        return [skill]
