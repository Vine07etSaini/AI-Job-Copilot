"""Data models for the profile, settings, jobs and LLM outputs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

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
    role_floor: float = 0.5  # score multiplier when the job title is outside your preferred roles


class PortalSettings(BaseModel):
    enabled: bool = True
    cdp_url: str = "http://127.0.0.1:9222"  # Chrome remote-debugging endpoint the portal attaches to
    close_chrome_on_finish: bool = True  # quit the debug Chrome when a run ends; it restarts on demand
    base_url: str = ""  # site to use when a portal has regional sites (Indeed: https://in.indeed.com)
    # Accept postings that show no date: the portal's own search filter (Indeed `fromage`) already limits their age.
    trust_search_age_filter: bool = False
    daily_cap: int | None = None  # applications per day on this portal; lower than per_portal_cap for strict sites
    easy_apply_only: bool = False  # search only jobs the portal can apply to in-site (LinkedIn "Easy Apply")
    pages_per_search: int = 2      # most pages to load per role x location query
    min_new_cards_per_page: int = 5  # a page adding fewer new cards than this means the site ran out of results
    min_delay_seconds: float = 3
    max_delay_seconds: float = 7


class Settings(BaseModel):
    match_threshold: int = 70
    max_job_age_days: int = 7
    skip_if_date_unknown: bool = True
    # A job filtered out this long ago may be looked at again, so a re-posted role is not blocked for ever.
    # 0 = never forget. Every other status (applied, rejected, pending review) always blocks.
    forget_filtered_after_days: int = 14
    score_weights: ScoreWeights = ScoreWeights()
    # review = every match waits for you | hybrid = auto-apply when your base resume already shows the job's key
    # skills, review only jobs that need a tailored resume | auto = apply to every match
    mode: Literal["review", "hybrid", "auto"] = "review"
    max_applications_per_day: int = 25
    per_portal_cap: int = 10
    max_jobs_per_run: int = 30
    search_card_target: int = 120  # cards a portal tries to collect per run; the pages opened are picked from these
    # One LLM call per portal per run re-reads the search cards before any job page is opened.
    # off = deterministic prescore order only | rank = the model re-orders | filter = it may also drop cards.
    triage: Literal["off", "rank", "filter"] = "rank"
    triage_threshold: int = 40     # filter mode only: cards the model scores below this are never opened
    triage_max_cards: int = 60     # cards sent in that one call; the rest keep their prescore order behind them
    apply_engine: str = "script"   # script = selectors + rules | cua = Gemini computer-use agent answers the drawer
    cua_max_steps: int = 25
    locked_sections: list[str] = ["experience", "education"]
    editable_sections: list[str] = ["skills", "projects"]
    max_keyword_repeats: int = 2
    redact_pii: bool = True        # mask name/email/phone/address/links in every LLM prompt
    portals: dict[str, PortalSettings] = {}
    send_daily_report: bool = True
    email_each_resume: bool = False
    send_recruiter_emails: str = "draft_only"
    blacklist_companies: list[str] = []
    exclude_title_words: list[str] = []  # skip cards whose title has any of these words ("senior", "intern")


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
    snippet: str = ""  # the preview a search card shows; `description` supersedes it after fetch_details
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
    min_years: float | None = Field(
        default=None,
        description="Years of this skill the JD asks for, e.g. 3 for '3+ years of Python'; null if not stated",
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


class TriagedCard(BaseModel):
    index: int = Field(description="The card's `i` from the list you were given")
    relevance: int = Field(description="0-100: how well this card fits the candidate's roles, skills and experience")
    reason: str = Field(description="One short line naming the deciding signal, in the card's own words")


class CardTriage(BaseModel):
    """The model's verdict on a batch of search cards; indices are reconciled against the real list, never trusted."""

    cards: list[TriagedCard]


class MatchResult(BaseModel):
    score: int
    matched_mandatory: list[str]
    missing_mandatory: list[str]
    matched_nice: list[str]
    experience_ok: bool
    role_fit: float = 1.0  # 1 = your roles or a generic title, ROLE_FIT_FAMILY = another stack, 0 = another job
    explanation: str
