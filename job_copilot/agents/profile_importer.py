"""Build a draft profile from a resume PDF (LLM extraction, verified against the PDF text)."""

from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from job_copilot.agents.skill_bank import find_term
from job_copilot.llm.chat import LLM
from job_copilot.llm.redact import mask_also
from job_copilot.models import ExtractedProject, Profile, Project, ResumeExtract, Skill

SYSTEM = """You read resumes precisely and copy only what the resume literally says.
- Never guess or add anything that is not in the text; leave a field empty instead.
- personal: contact details exactly as written (empty string if absent).
- summary: the resume's own summary/objective, or an empty string if it has none.
- skills: concrete skills and tools named in the resume, short canonical names ("PostgreSQL", "Docker").
- projects: name, technologies used, and a description in the resume's own words.
- experience: one entry per job, bullets copied verbatim; start/end exactly as written (e.g. "Jan 2022", "Present").
- education: degree, institution and year as written.
- total_experience_years: only if the resume states it or the job dates make it unambiguous, else null."""


def extract_pdf_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"Resume not found: {path}")
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if not text:
        raise ValueError(f"No text found in {path.name}. Scanned/image-only PDFs are not supported.")
    if links := _hyperlinks(reader):
        # link text like "GitHub" hides the real URL, which a PDF keeps in an annotation
        text += "\n\nHyperlinks in this PDF:\n" + "\n".join(links)
    return text


def _hyperlinks(reader: PdfReader) -> list[str]:
    urls: dict[str, None] = {}
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            if uri := (annot.get_object().get("/A") or {}).get("/URI"):
                urls[str(uri)] = None
    return list(urls)


def name_guess(text: str) -> str:
    """The resume's first line when it looks like a name, so it is masked before the profile holds it."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first if len(first.split()) <= 4 and not re.search(r"[\d@/|:]", first) else ""


def extract_profile(text: str, llm: LLM) -> ResumeExtract:
    llm = mask_also(llm, name=name_guess(text))
    return llm.generate_json(f"Resume text:\n\n{text}", ResumeExtract, system=SYSTEM, temperature=0.0)


def verify_skills(extract: ResumeExtract, text: str) -> tuple[ResumeExtract, list[str]]:
    """Keep only skills that literally appear in the resume text, so the model can't add to the skill bank."""
    kept, dropped, seen = [], [], set()
    for skill in (s.strip() for s in extract.skills):
        if not skill or skill.lower() in seen:
            continue
        seen.add(skill.lower())
        (kept if find_term(text, skill) else dropped).append(skill)
    return extract.model_copy(update={"skills": kept}), dropped


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _merge_project(ep: ExtractedProject, existing: dict[str, Project]) -> Project:
    old = existing.get(ep.name.lower())
    return Project(
        id=old.id if old else _slug(ep.name), name=ep.name, github=ep.github or (old.github if old else ""),
        tech=ep.tech, on_resume=old.on_resume if old else True, description=ep.description,
    )


def merge_into_profile(extract: ResumeExtract, existing: Profile) -> Profile:
    """Resume data replaces personal/summary/skills/projects/experience/education; the rest is kept as is."""
    old_skills = {s.name.lower(): s for s in existing.skills}
    old_projects = {p.name.lower(): p for p in existing.projects}
    return existing.model_copy(update={
        "personal": extract.personal,
        "summary": extract.summary or existing.summary,
        "total_experience_years": extract.total_experience_years or existing.total_experience_years,
        # aliases carry over for skills you already had; years are never inferred, so fill them in by hand
        "skills": [Skill(name=s, aliases=getattr(old_skills.get(s.lower()), "aliases", [])) for s in extract.skills],
        "projects": [_merge_project(p, old_projects) for p in extract.projects],
        "experience": extract.experience,
        "education": extract.education,
    })


def empty_fields(profile: Profile) -> list[str]:
    """Important fields the resume did not provide, so the user knows what to fill in by hand."""
    checks = {
        "personal.phone": profile.personal.phone, "personal.linkedin": profile.personal.linkedin,
        "personal.github": profile.personal.github, "summary": profile.summary,
        "projects": profile.projects, "experience": profile.experience, "education": profile.education,
    }
    return [name for name, value in checks.items() if not value]
