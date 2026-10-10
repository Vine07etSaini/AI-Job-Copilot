"""Endpoints that run the pipeline (search, apply, analyze, report, skill gaps)."""

from __future__ import annotations

from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps import get_tracker
from backend.routers import login
from backend.schemas import AnalyzeRequest, ApplyRequest, SearchRequest, SkillGapResponse
from job_copilot import pipeline
from job_copilot.agents.jd_analyzer import analyze_jd
from job_copilot.agents.matcher import score_job
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import get_profile, get_settings
from job_copilot.db import Tracker
from job_copilot.models import JobPosting

router = APIRouter(prefix="/api", tags=["actions"])

TrackerDep = Annotated[Tracker, Depends(get_tracker)]


def _not_during_login() -> None:
    if login.is_running():
        raise HTTPException(409, "A Naukri login is in progress; finish it first.")


@router.post("/search")
async def run_search(body: SearchRequest) -> dict:
    """Search a portal, score postings and tailor resumes. 'naukri' can take several minutes."""
    _not_during_login()
    return await pipeline.discover(body.source, body.limit)


@router.post("/apply")
async def apply_approved(body: ApplyRequest) -> dict:
    """Apply to approved jobs, respecting the daily and per-portal caps. Only call on explicit user action."""
    _not_during_login()
    return await pipeline.apply_approved(body.max_count)


@router.post("/analyze")
def analyze(body: AnalyzeRequest) -> dict:
    """Analyze and score a pasted job description without saving anything."""
    profile = get_profile()
    bank = SkillBank(profile)
    posting = JobPosting(portal="adhoc", external_id="adhoc", title=body.title or "Unknown role",
                         company=body.company or "Unknown", url="", description=body.description)
    analysis = analyze_jd(posting, bank, pipeline._llm())
    match = score_job(profile, analysis, bank, get_settings().score_weights)
    return {"analysis": analysis.model_dump(), "match": match.model_dump()}


@router.post("/report")
def report(send_email: bool = Query(False)) -> dict:
    return pipeline.daily_report(send=send_email)


@router.get("/skill-gaps", response_model=SkillGapResponse)
def skill_gaps(tracker: TrackerDep, top: int = Query(10, ge=1, le=50)):
    """Most-demanded mandatory skills across analyzed jobs that are missing from the skill bank."""
    bank = SkillBank(get_profile())
    demand: Counter[str] = Counter()
    jobs = [j for j in tracker.list(limit=1000) if j.get("analysis")]
    for j in jobs:
        for req in j["analysis"]["mandatory_skills"]:
            if not any(bank.match(t) for t in (req["skill"], *req["alternatives"])):
                demand[req["skill"]] += 1
    return {
        "jobs_analyzed": len(jobs),
        "missing_skills": [{"skill": s, "jobs": n, "learning": bank.is_learning(s)} for s, n in demand.most_common(top)],
    }
