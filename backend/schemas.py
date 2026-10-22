"""Request/response models of the HTTP API (the job_copilot models stay internal)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobSummary(BaseModel):
    id: str
    title: str
    company: str
    portal: str
    location: str | None = None
    url: str
    experience_text: str | None = None
    salary_text: str | None = None
    match_score: int | None = None
    status: str
    status_reason: str | None = None
    resume_path: str | None = None
    updated_at: str


class JobDetail(JobSummary):
    description: str | None = None
    recruiter_email: str | None = None
    analysis: dict | None = None
    match: dict | None = None
    guard: dict | None = None


class StatsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    applied_today: int


class SearchRequest(BaseModel):
    source: str = Field(default="sample", description="'sample' (offline), 'naukri', 'indeed', 'linkedin' or 'all' (open a browser)")
    limit: int = Field(default=10, ge=1, le=50)


class ApplyRequest(BaseModel):
    max_count: int = Field(default=5, ge=1, le=25)


class RejectRequest(BaseModel):
    reason: str = "rejected by user"


class AnalyzeRequest(BaseModel):
    description: str = Field(min_length=20)
    title: str = ""
    company: str = ""


class StatusChange(BaseModel):
    id: str
    status: str


class JobDeleted(BaseModel):
    id: str
    deleted: bool = True


class SkillGap(BaseModel):
    skill: str
    jobs: int
    learning: bool


class SkillGapResponse(BaseModel):
    jobs_analyzed: int
    missing_skills: list[SkillGap]


class ProjectUpdate(BaseModel):
    description: str = Field(min_length=1)
