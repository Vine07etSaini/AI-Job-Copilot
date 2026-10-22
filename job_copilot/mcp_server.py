"""MCP server exposing the Job Copilot as tools.

Used by the built-in Gemini agent (`python main.py chat`) and by any MCP client
(Claude Desktop, Claude Code, Cursor, ...). Run directly with:

    python -m job_copilot.mcp_server
"""

from __future__ import annotations

import hashlib
from collections import Counter

import yaml
from mcp.server.mcpserver import MCPServer

from job_copilot import pipeline
from job_copilot.agents.honesty_guard import _unsupported_claims
from job_copilot.agents.jd_analyzer import analyze_jd
from job_copilot.agents.matcher import score_job
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import get_profile, get_settings, save_profile, setup_logging
from job_copilot.db import Tracker
from job_copilot.llm.redact import profile_for_llm
from job_copilot.models import JobPosting, JobStatus

server = MCPServer(
    name="job-copilot",
    instructions=(
        "Tools for an AI job-application copilot. The user's profile is the single source of truth: "
        "never invent skills or experience. Experience and education are read-only. In review mode the "
        "user must approve each job before it is applied to - only call approve_job when the user asks."
    ),
)


def _compact(j: dict) -> dict:
    return {
        "id": j["id"], "title": j["title"], "company": j["company"], "portal": j["portal"],
        "location": j["location"], "match_score": j["match_score"], "status": j["status"],
        "reason": j["status_reason"], "explanation": (j.get("match") or {}).get("explanation"),
        "resume_path": j["resume_path"], "url": j["url"],
    }


def _get_or_error(job_id: str) -> dict:
    job = Tracker().get(job_id)
    if job is None:
        raise ValueError(f"No job with id '{job_id}'. Use list_jobs to find ids.")
    return job


@server.tool()
def get_profile_summary() -> dict:
    """Get the user's profile: skills (Verified Skill Bank), projects, experience, preferences.

    Contact details and links are left out: this output goes to an LLM.
    """
    return profile_for_llm(get_profile())


@server.tool()
def list_jobs(status: str | None = None, limit: int = 20) -> list[dict]:
    """List tracked jobs, best match first.

    status: optional filter - one of filtered_out, pending_review, approved, rejected, applied, needs_manual, failed.
    """
    return [_compact(j) for j in Tracker().list(status=status, limit=limit)]


@server.tool()
def get_job(job_id: str) -> dict:
    """Full details of one job: description, JD analysis, match breakdown, honesty-guard report, resume path."""
    return _get_or_error(job_id)


@server.tool()
async def run_job_search(source: str = "naukri", limit: int = 10) -> dict:
    """Search a portal for fresh jobs, score them and tailor resumes for good matches.

    source: 'naukri', 'indeed' or 'linkedin' (open a browser; can take several minutes) or 'sample' (offline data).
    limit: max number of job pages to open.
    """
    return await pipeline.discover(source, limit)


@server.tool()
async def rescore_jobs() -> dict:
    """Re-score jobs that missed the match threshold against the current profile (e.g. after a skill was added).

    Opens no pages; jobs that now pass get a tailored resume and wait for review.
    """
    return await pipeline.rescore()


@server.tool()
def analyze_job_description(description: str, title: str = "", company: str = "") -> dict:
    """Analyze any job description text and score it against the profile, without saving anything."""
    profile = get_profile()
    bank = SkillBank(profile)
    posting = JobPosting(portal="adhoc", external_id="adhoc", title=title or "Unknown role",
                         company=company or "Unknown", url="", description=description)
    analysis = analyze_jd(posting, bank, pipeline._llm())
    if not title:  # judge the role by the title the JD itself gives, not by the placeholder
        posting.title = analysis.role_title
    match = score_job(profile, analysis, bank, get_settings().score_weights, posting)
    return {"analysis": analysis.model_dump(), "match": match.model_dump()}


@server.tool()
async def add_job_manually(title: str, company: str, url: str, description: str, location: str = "") -> dict:
    """Track a job the user found themselves: analyze, score and tailor a resume if it clears the threshold."""
    ext = hashlib.sha1(url.encode() or description.encode()).hexdigest()[:12]
    posting = JobPosting(portal="manual", external_id=ext, title=title, company=company, url=url,
                         location=location, description=description)
    jid, status = await pipeline.process_posting(posting, Tracker(), pipeline._llm())
    return _compact(Tracker().get(jid)) | {"status": str(status)}


@server.tool()
def approve_job(job_id: str) -> dict:
    """Approve a pending_review job so it will be applied to. Only call when the user explicitly approves."""
    job = _get_or_error(job_id)
    if job["status"] not in (JobStatus.PENDING_REVIEW, JobStatus.REJECTED, JobStatus.NEEDS_MANUAL):
        raise ValueError(f"Job is '{job['status']}', only pending_review/rejected/needs_manual jobs can be approved.")
    Tracker().set_status(job_id, JobStatus.APPROVED, "approved by user")
    return {"id": job_id, "status": "approved"}


@server.tool()
def reject_job(job_id: str, reason: str = "rejected by user") -> dict:
    """Reject a job so it is never applied to."""
    _get_or_error(job_id)
    Tracker().set_status(job_id, JobStatus.REJECTED, reason)
    return {"id": job_id, "status": "rejected"}


@server.tool()
async def apply_approved_jobs(max_count: int = 5) -> dict:
    """Apply to approved jobs (opens a browser), respecting daily and per-portal caps."""
    return await pipeline.apply_approved(max_count)


@server.tool()
def generate_daily_report(send_email: bool = False) -> dict:
    """Build today's Excel report (optionally email it). Returns the file path and a text summary."""
    return pipeline.daily_report(send=send_email)


@server.tool()
def skill_gap_report(top: int = 10) -> dict:
    """Most-demanded skills across analyzed jobs that are missing from the user's skill bank."""
    profile = get_profile()
    bank = SkillBank(profile)
    demand: Counter[str] = Counter()
    jobs = [j for j in Tracker().list(limit=1000) if j.get("analysis")]
    for j in jobs:
        for req in j["analysis"]["mandatory_skills"]:
            if not any(bank.match(t) for t in (req["skill"], *req["alternatives"])):
                demand[req["skill"]] += 1
    return {
        "jobs_analyzed": len(jobs),
        "missing_skills": [{"skill": s, "jobs": n, "learning": bank.is_learning(s)} for s, n in demand.most_common(top)],
    }


@server.tool()
def update_project_description(project_id: str, description: str) -> dict:
    """Edit one project's description in the profile (an allowed, editable section).

    Rejected if it introduces skills that are not in the Verified Skill Bank or the project's own tech.
    """
    profile = get_profile()
    proj = next((p for p in profile.projects if p.id == project_id), None)
    if proj is None:
        raise ValueError(f"Unknown project '{project_id}'. Known: {[p.id for p in profile.projects]}")
    bank = SkillBank(profile)
    known_terms = {req["skill"] for j in Tracker().list(limit=1000) if j.get("analysis")
                   for req in j["analysis"]["mandatory_skills"]}
    known_terms.update(profile.learning_goals)
    if bad := _unsupported_claims(description, proj.description, proj.tech, sorted(known_terms), bank):
        raise ValueError(f"Rejected by Honesty Guard - unverified skills: {', '.join(bad)}")
    proj.description = description.strip()
    save_profile(profile)
    return {"project_id": project_id, "description": proj.description}


@server.resource("profile://yaml", mime_type="text/yaml")
def profile_yaml() -> str:
    """The profile YAML without contact details and links (MCP clients pass resources to their LLM)."""
    return yaml.safe_dump(profile_for_llm(get_profile()), sort_keys=False)


def main() -> None:
    setup_logging()
    server.run()


if __name__ == "__main__":
    main()
