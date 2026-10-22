"""Extract structured requirements from a job description (LLM, with a heuristic fallback)."""

from __future__ import annotations

import logging
import re

from job_copilot.agents.skill_bank import SkillBank, find_term
from job_copilot.llm.chat import LLM
from job_copilot.models import JDAnalysis, JobPosting, SkillRequirement

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}\b")
EXP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:yrs|years|yr)", re.I)
EXP_MIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs|years|yr)", re.I)
YEARS = r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b"
# A line that starts a "nice to have" block, and one that starts a "required" block again.
PREFERRED_RE = re.compile(r"\b(nice to have|good to have|preferred|bonus|added advantage|is a plus|would be a plus)\b",
                          re.I)
REQUIRED_RE = re.compile(r"\b(must have|must-have|required|requirements|mandatory|qualifications|what you need)\b",
                         re.I)
# Portal tags that are not technical skills; as "mandatory" they would only add noise.
SOFT_SKILLS = {"communication", "communication skills", "teamwork", "team player", "problem solving", "leadership",
               "analytical skills", "english", "it", "software development", "computer science", "coding",
               "programming", "development", "technical skills", "interpersonal skills", "time management"}

SYSTEM = """You are an expert technical recruiter who reads job descriptions precisely.
Extract only what the job description actually says. Do not guess or add skills that are not mentioned.
- mandatory_skills: concrete technical skills/tools that are required ("must have", "required", core stack).
  If the JD accepts alternatives (e.g. "AWS or GCP"), put one in `skill` and the rest in `alternatives`.
- nice_to_have_skills: skills marked as preferred, good to have, or a plus.
- Use short canonical skill names ("PostgreSQL", "Docker", "REST APIs"), not sentences.
- min_years on a skill: only when the JD asks for years of that specific skill ("3+ years of Python"), else null.
- Experience: numbers in years; null if not stated.
- recruiter_email / recruiter_phone: only if literally present in the text, else null."""


def analyze_jd(posting: JobPosting, bank: SkillBank, llm: LLM | None = None) -> JDAnalysis:
    if llm is not None:
        prompt = (
            f"Job title: {posting.title}\nCompany: {posting.company}\n"
            f"Experience (from listing): {posting.experience_text or 'n/a'}\n"
            f"Tags: {', '.join(posting.tags) or 'n/a'}\n\n"
            f"Job description:\n{posting.description[:15000]}"
        )
        analysis = llm.generate_json(prompt, JDAnalysis, system=SYSTEM)
    else:
        log.info("No LLM configured; using heuristic JD analysis for %s", posting.title)
        analysis = heuristic_analysis(posting, bank)
    return _fill_from_text(analysis, posting)


def _sections(description: str) -> tuple[str, str]:
    """Split a JD into (required text, nice-to-have text) by its headings; everything is required by default."""
    required, preferred, in_preferred = [], [], False
    for line in description.splitlines():
        if PREFERRED_RE.search(line):
            in_preferred = True
        elif REQUIRED_RE.search(line):
            in_preferred = False
        (preferred if in_preferred else required).append(line)
    return "\n".join(required), "\n".join(preferred)


def heuristic_analysis(posting: JobPosting, bank: SkillBank) -> JDAnalysis:
    """Offline fallback: a skill is mandatory when the title or the required part of the JD names it.

    Candidates are the portal tags (minus soft skills) and your skill-bank skills found in the text. A tag the
    description never mentions, or one only in a "nice to have" block, is nice-to-have: recruiters over-tag.
    """
    required, preferred = _sections(posting.description)
    tags = [t for t in dict.fromkeys(t.strip() for t in posting.tags) if t and t.lower() not in SOFT_SKILLS]
    found = bank.skills_in(posting.description)
    mandatory, nice, seen = [], [], set()
    for skill in [*tags, *found]:
        canonical = bank.canonical(skill) or skill
        if canonical.lower() in seen:
            continue
        seen.add(canonical.lower())
        forms = bank.surface_forms(canonical) if bank.canonical(skill) else [skill]
        if any(find_term(posting.title, f) or find_term(required, f) for f in forms):
            mandatory.append(skill)
        elif skill in tags or any(find_term(preferred, f) for f in forms):
            nice.append(skill)
    return JDAnalysis(
        role_title=posting.title,
        mandatory_skills=[SkillRequirement(skill=s, alternatives=[]) for s in mandatory],
        nice_to_have_skills=nice,
        keywords=[*mandatory, *nice],
        summary=posting.description[:200],
    )


def _fill_from_text(a: JDAnalysis, p: JobPosting) -> JDAnalysis:
    text = p.description
    if not a.recruiter_email and (m := EMAIL_RE.search(text)):
        a.recruiter_email = m.group(0).rstrip(".")
    if not a.recruiter_phone and (m := PHONE_RE.search(text)):
        a.recruiter_phone = m.group(0)
    if a.min_experience_years is None:
        lo, hi = parse_experience(p.experience_text or text)
        a.min_experience_years, a.max_experience_years = lo, a.max_experience_years or hi
    for req in a.mandatory_skills:
        if req.min_years is None:
            req.min_years = skill_years(text, req.skill)
    return a


def skill_years(text: str, skill: str) -> float | None:
    """Years the JD asks for one skill ("3+ years of Python", "Python (2 yrs)"), within one sentence."""
    s = rf"(?<![A-Za-z0-9]){re.escape(skill)}(?![A-Za-z0-9+#])"
    years = rf"(?<![-\d.]){YEARS}"  # not the end of a total-experience range like "2-5 years"
    for pattern in (rf"{years}[^.\n]{{0,30}}?{s}", rf"{s}[^.\n]{{0,15}}?{years}"):
        if m := re.search(pattern, text, re.I):
            return float(m.group(1))
    return None


def parse_experience(text: str) -> tuple[float | None, float | None]:
    if m := EXP_RE.search(text):
        return float(m.group(1)), float(m.group(2))
    if m := EXP_MIN_RE.search(text):
        return float(m.group(1)), None
    return None, None
