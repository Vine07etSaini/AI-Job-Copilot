"""Data models for the profile, settings, jobs and LLM outputs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- profile


class Personal(BaseModel):
    name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Skill(BaseModel):
    name: str
    aliases: list[str] = []
    years: float | None = None


class Project(BaseModel):
    id: str
    name: str
    github: str = ""
    tech: list[str] = []
    on_resume: bool = True
    description: str


class Experience(BaseModel):
    title: str
    company: str
    location: str = ""
    start: str
    end: str
    bullets: list[str] = []


class Education(BaseModel):
    degree: str
    institution: str
    year: str = ""


class Preferences(BaseModel):
    roles: list[str]
    locations: list[str] = []
    min_salary_lpa: float | None = None
    notice_period_days: int | None = None
    # Used only to answer portal screening questions; left unset, those questions go to you.
    expected_ctc_lpa: float | None = None
    current_ctc_lpa: float | None = None
    relocate: bool | None = None


class Profile(BaseModel):
    personal: Personal
    summary: str
    total_experience_years: float
    skills: list[Skill]
    equivalents: dict[str, list[str]] = {}
    learning_goals: list[str] = []
    projects: list[Project] = []
    experience: list[Experience] = []
    education: list[Education] = []
    preferences: Preferences


class ExtractedProject(BaseModel):
    name: str
    tech: list[str] = []
    description: str = ""
    github: str = ""


class ResumeExtract(BaseModel):
    """What can be read off a resume PDF (no preferences, equivalents or learning goals: resumes have none)."""

    personal: Personal
    summary: str = ""
    total_experience_years: float | None = None
    skills: list[str] = []
    projects: list[ExtractedProject] = []
    experience: list[Experience] = []
    education: list[Education] = []


# ---------------------------------------------------------------- settings


class ScoreWeights(BaseModel):
    mandatory_skills: float = 0.6
    nice_to_have_skills: float = 0.2
    experience: float = 0.2


class PortalSettings(BaseModel):
    enabled: bool = True
    cdp_url: str = "http://127.0.0.1:9222"  # Chrome remote-debugging endpoint the portal attaches to
    close_chrome_on_finish: bool = True  # quit the debug Chrome when a run ends; it restarts on demand
    headless: bool = False  # (unused while attached) True = no window; Naukri blocks this
    hide_window: bool = True  # headed but parked off-screen; watch it in the web UI live view
    browser_channel: str = "chrome"  # installed Chrome; "" = bundled Chromium
    pages_per_search: int = 2
    min_delay_seconds: float = 3
    max_delay_seconds: float = 7


class Settings(BaseModel):
    match_threshold: int = 70
    max_job_age_days: int = 7
    skip_if_date_unknown: bool = True
    score_weights: ScoreWeights = ScoreWeights()
    mode: str = "review"
    max_applications_per_day: int = 25
    per_portal_cap: int = 10
    max_jobs_per_run: int = 30
    apply_engine: str = "script"   # script = selectors + rules | cua = Gemini computer-use agent answers the drawer
    cua_max_steps: int = 25
    locked_sections: list[str] = ["experience", "education"]
    editable_sections: list[str] = ["skills", "projects"]
    max_keyword_repeats: int = 2
    portals: dict[str, PortalSettings] = {}
    send_daily_report: bool = True
    email_each_resume: bool = False
    send_recruiter_emails: str = "draft_only"
    blacklist_companies: list[str] = []


# ---------------------------------------------------------------- jobs


class JobStatus(StrEnum):
    FILTERED_OUT = "filtered_out"      # below threshold / stale / blacklisted
    PENDING_REVIEW = "pending_review"  # resume ready, waiting for your approval
    APPROVED = "approved"              # you approved; will be applied
    REJECTED = "rejected"              # you rejected
    APPLIED = "applied"
    NEEDS_MANUAL = "needs_manual"      # external site / questionnaire / captcha
    FAILED = "failed"


class JobPosting(BaseModel):
    portal: str
    external_id: str
    title: str
    company: str
    url: str
    location: str = ""
    experience_text: str = ""
    salary_text: str = ""
    posted_text: str = ""
    posted_at: datetime | None = None
    tags: list[str] = []
    description: str = ""
    recruiter_email: str | None = None
    recruiter_phone: str | None = None


# ---------------------------------------------------------------- LLM outputs
# Kept flat and simple so they can be passed directly as Gemini response schemas.


class SkillRequirement(BaseModel):
    skill: str = Field(description="Required skill, e.g. 'AWS'")
    alternatives: list[str] = Field(
        description="Skills the JD accepts instead, e.g. ['GCP'] for 'AWS or GCP'. Empty if none."
    )


class JDAnalysis(BaseModel):
    role_title: str
    mandatory_skills: list[SkillRequirement]
    nice_to_have_skills: list[str]
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    keywords: list[str] = Field(description="Important ATS keywords/phrases from the JD")
    recruiter_email: str | None = None
    recruiter_phone: str | None = None
    summary: str = Field(description="Two-sentence summary of the role")


class ProjectRewrite(BaseModel):
    project_id: str
    description: str


class TailoredContent(BaseModel):
    summary: str
    skills: list[str] = Field(description="Skills from the skill bank, most relevant first")
    projects: list[ProjectRewrite]
    keywords_used: list[str]


class MatchResult(BaseModel):
    score: int
    matched_mandatory: list[str]
    missing_mandatory: list[str]
    matched_nice: list[str]
    experience_ok: bool
    explanation: str
