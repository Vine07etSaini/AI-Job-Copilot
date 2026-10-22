"""Job tracker endpoints: list, detail, stats, approve / reject, delete."""

from __future__ import annotations

from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps import get_tracker
from backend.schemas import JobDeleted, JobDetail, JobSummary, RejectRequest, StatsResponse, StatusChange
from job_copilot.agents.resume_builder import remove_resume
from job_copilot.db import Tracker
from job_copilot.models import JobStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

TrackerDep = Annotated[Tracker, Depends(get_tracker)]
APPROVABLE = (JobStatus.PENDING_REVIEW, JobStatus.REJECTED, JobStatus.NEEDS_MANUAL)


def _get_or_404(tracker: Tracker, job_id: str) -> dict:
    if (job := tracker.get(job_id)) is None:
        raise HTTPException(404, f"No job with id '{job_id}'.")
    return job


@router.get("", response_model=list[JobSummary])
def list_jobs(tracker: TrackerDep, status: JobStatus | None = None, limit: int = Query(100, ge=1, le=500)):
    return tracker.list(status=str(status) if status else None, limit=limit)


@router.get("/stats", response_model=StatsResponse)
def stats(tracker: TrackerDep):
    jobs = tracker.list(limit=10_000)
    return StatsResponse(
        total=len(jobs),
        by_status=dict(Counter(j["status"] for j in jobs)),
        applied_today=tracker.applied_count_today(),
    )


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: str, tracker: TrackerDep):
    return _get_or_404(tracker, job_id)


@router.post("/{job_id}/approve", response_model=StatusChange)
def approve_job(job_id: str, tracker: TrackerDep):
    """Mark a job approved. Applying still needs a separate, explicit POST /api/apply."""
    job = _get_or_404(tracker, job_id)
    if job["status"] not in APPROVABLE:
        raise HTTPException(409, f"Job is '{job['status']}', only pending_review/rejected/needs_manual can be approved.")
    tracker.set_status(job_id, JobStatus.APPROVED, "approved by user")
    return StatusChange(id=job_id, status=str(JobStatus.APPROVED))


@router.post("/{job_id}/reject", response_model=StatusChange)
def reject_job(job_id: str, body: RejectRequest, tracker: TrackerDep):
    _get_or_404(tracker, job_id)
    tracker.set_status(job_id, JobStatus.REJECTED, body.reason)
    return StatusChange(id=job_id, status=str(JobStatus.REJECTED))


@router.delete("/{job_id}", response_model=JobDeleted)
def delete_job(job_id: str, tracker: TrackerDep):
    """Delete a job and its tailored resume. Applied jobs are kept: their record stops a second application."""
    job = _get_or_404(tracker, job_id)
    if job["status"] == JobStatus.APPLIED:
        raise HTTPException(409, "Applied jobs can't be deleted: the record stops the same job being applied to twice.")
    if job["resume_path"]:
        remove_resume(job["resume_path"])
    tracker.delete(job_id)
    return JobDeleted(id=job_id)
