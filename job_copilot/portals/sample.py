"""Offline portal backed by data/sample_jobs.json - for testing the pipeline without a browser."""

from __future__ import annotations

import json

from job_copilot.agents.freshness import parse_posted
from job_copilot.config import DATA_DIR
from job_copilot.models import JobPosting, JobStatus
from job_copilot.portals.base import Portal


class SamplePortal(Portal):
    name = "sample"

    async def search(self, role: str, location: str, max_age_days: int, experience_years: float,
                     target_cards: int = 0) -> list[JobPosting]:
        jobs = json.loads((DATA_DIR / "sample_jobs.json").read_text(encoding="utf-8"))
        return [JobPosting(portal=self.name, posted_at=parse_posted(j.get("posted_text")), **j) for j in jobs]

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        return posting  # sample jobs already include the description

    async def apply(self, url: str) -> tuple[JobStatus, str]:
        return JobStatus.APPLIED, "simulated apply (sample portal)"
