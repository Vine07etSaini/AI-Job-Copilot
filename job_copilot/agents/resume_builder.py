"""Tailor resume content with the LLM, run it through the Honesty Guard and render a PDF."""

from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from job_copilot.agents.honesty_guard import GuardReport, guard
from job_copilot.agents.skill_bank import SkillBank, find_term
from job_copilot.config import IST, RESUMES_DIR, TEMPLATES_DIR, ensure_dirs
from job_copilot.llm.chat import LLM
from job_copilot.models import JDAnalysis, JobPosting, MatchResult, Profile, ProjectRewrite, TailoredContent

log = logging.getLogger(__name__)

SYSTEM = """You tailor a candidate's resume to a job description. You are scrupulously honest.
Hard rules:
- Only mention skills/tools from VERIFIED_SKILLS. Never invent skills, tools, metrics, employers or experience.
- If the JD accepts alternatives (e.g. "AWS or GCP") and the candidate has one, use the one they have.
- Rewrite each project description (1-3 sentences) to highlight tech that is relevant to the JD,
  using only that project's own tech and facts already in its description.
- Keep numbers/metrics exactly as given. Do not add new ones.
- Keywords must read naturally. Never repeat the same keyword more than twice in one project.
- `skills`: reorder VERIFIED_SKILLS so the most JD-relevant come first. Do not add any.
- `summary`: 2-3 sentences, first person implied (no "I"), based only on the original summary and verified skills.
- `keywords_used`: JD keywords you actually worked in."""


def base_content(profile: Profile) -> TailoredContent:
    return TailoredContent(
        summary=profile.summary,
        skills=[s.name for s in profile.skills],
        projects=[ProjectRewrite(project_id=p.id, description=p.description) for p in profile.projects if p.on_resume],
        keywords_used=[],
    )


def resume_gaps(profile: Profile, match: MatchResult, bank: SkillBank) -> list[str]:
    """Matched must-have skills your base resume never shows outside its skills list: the reason to tailor it.

    The skills list is ignored because it holds every skill you have; recruiters and ATS look for them in the
    summary, projects and experience.
    """
    text = "\n".join([
        profile.summary,
        *(f"{p.name} {' '.join(p.tech)} {p.description}" for p in profile.projects if p.on_resume),
        *(f"{e.title} {' '.join(e.bullets)}" for e in profile.experience),
    ])
    skills = dict.fromkeys(m.split(" (for ")[0] for m in match.matched_mandatory)
    return [s for s in skills if not any(find_term(text, f) for f in bank.surface_forms(s))]


def tailor(profile: Profile, posting: JobPosting, analysis: JDAnalysis, match: MatchResult, bank: SkillBank,
           llm: LLM | None, max_repeats: int = 2) -> tuple[TailoredContent, GuardReport]:
    if llm is None:
        draft = _reorder_only(profile, match)
    else:
        projects = [p.model_dump(include={"id", "name", "tech", "description"}) for p in profile.projects if p.on_resume]
        prompt = json.dumps({
            "JOB": {"title": posting.title, "company": posting.company},
            "JD_ANALYSIS": analysis.model_dump(),
            "VERIFIED_SKILLS": bank.names,
            "APPROVED_EQUIVALENTS": profile.equivalents,
            "ORIGINAL_SUMMARY": profile.summary,
            "PROJECTS": projects,
        }, indent=2)
        draft = llm.generate_json(prompt, TailoredContent, system=SYSTEM, temperature=0.3)
    return guard(draft, profile, analysis, bank, max_repeats=max_repeats)


def _reorder_only(profile: Profile, match: MatchResult) -> TailoredContent:
    """Offline fallback: no rewriting, just surface the matched skills first."""
    content = base_content(profile)
    first = [m.split(" (for ")[0] for m in match.matched_mandatory + match.matched_nice]
    content.skills = list(dict.fromkeys(first + content.skills))
    return content


# ------------------------------------------------------------------ rendering

_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html", "j2"]))


def render_html(profile: Profile, content: TailoredContent) -> str:
    by_id = {p.id: p for p in profile.projects}
    projects = [
        {"name": by_id[rw.project_id].name, "github": by_id[rw.project_id].github,
         "tech": by_id[rw.project_id].tech, "description": rw.description}
        for rw in content.projects if rw.project_id in by_id
    ]
    # experience/education come straight from the profile: they never pass through the LLM
    return _env.get_template("resume.html.j2").render(
        p=profile.personal, summary=content.summary, skills=content.skills, projects=projects,
        experience=profile.experience, education=profile.education,
    )


async def html_to_pdf(html: str, out: Path) -> Path:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.pdf(path=str(out), format="A4", print_background=True,
                       margin={"top": "14mm", "bottom": "14mm", "left": "14mm", "right": "14mm"})
        await browser.close()
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def resume_as_text(profile: Profile, content: TailoredContent) -> list[str]:
    by_id = {p.id: p.name for p in profile.projects}
    lines = ["SUMMARY", content.summary, "SKILLS", ", ".join(content.skills), "PROJECTS"]
    for rw in content.projects:
        lines += [by_id.get(rw.project_id, rw.project_id), rw.description]
    return lines


def remove_resume(resume_path: str) -> None:
    """Delete a tailored PDF and its vault JSON; anything outside the resume folder is left alone."""
    pdf = Path(resume_path).resolve()
    if not pdf.is_relative_to(RESUMES_DIR.resolve()):
        log.warning("Not deleting %s: outside %s", pdf, RESUMES_DIR)
        return
    for f in (pdf, pdf.with_suffix(".json")):
        f.unlink(missing_ok=True)


async def build_resume(profile: Profile, posting: JobPosting, content: TailoredContent, job_id: str,
                       analysis: JDAnalysis | None = None, report: GuardReport | None = None) -> tuple[Path, str]:
    """Render the PDF and store it in the resume vault with the JD and a diff. Returns (path, url)."""
    ensure_dirs()
    stamp = datetime.now(IST).strftime("%Y%m%d")
    stem = f"{stamp}_{_slug(posting.company)}_{_slug(posting.title)}_{job_id}"
    pdf = await html_to_pdf(render_html(profile, content), RESUMES_DIR / f"{stem}.pdf")

    diff = list(difflib.unified_diff(resume_as_text(profile, base_content(profile)),
                                     resume_as_text(profile, content), "base", "tailored", lineterm=""))
    vault = {
        "job": posting.model_dump(mode="json", exclude={"description"}),
        "job_description": posting.description,
        "analysis": analysis.model_dump() if analysis else None,
        "tailored": content.model_dump(),
        "guard": report.model_dump() if report else None,
        "diff_vs_base": diff,
        "created_at": datetime.now(IST).isoformat(timespec="seconds"),
    }
    (RESUMES_DIR / f"{stem}.json").write_text(json.dumps(vault, indent=2), encoding="utf-8")
    # Phase 1 stores resumes locally; swap this for Google Drive / S3 to get a shareable URL.
    return pdf, pdf.resolve().as_uri()
