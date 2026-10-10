"""Paths, settings and profile loading."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

from job_copilot.models import Profile, Settings

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
TEMPLATES_DIR = ROOT / "templates"
DATA_DIR = ROOT / "data"
STORAGE_DIR = ROOT / "storage"
RESUMES_DIR = STORAGE_DIR / "resumes"
REPORTS_DIR = STORAGE_DIR / "reports"
DB_PATH = STORAGE_DIR / "tracker.db"
PROFILE_PATH = CONFIG_DIR / "profile.yaml"
PROFILE_DRAFT_PATH = CONFIG_DIR / "profile.imported.yaml"

IST = timezone(timedelta(hours=5, minutes=30), "IST")

load_dotenv(ROOT / ".env")


def ensure_dirs() -> None:
    for d in (RESUMES_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_settings() -> Settings:
    return Settings.model_validate(_load_yaml(CONFIG_DIR / "settings.yaml"))


def get_profile(path: Path = PROFILE_PATH) -> Profile:
    # Not cached: the agent may edit project descriptions at runtime.
    return Profile.model_validate(_load_yaml(path))


def save_profile(profile: Profile, path: Path = PROFILE_PATH) -> None:
    data = profile.model_dump(mode="json", exclude_none=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=100)


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    user: str
    password: str
    to: str

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.to)


def get_email_config() -> EmailConfig:
    return EmailConfig(
        host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        port=int(os.getenv("SMTP_PORT", "587")),
        user=os.getenv("SMTP_USER", ""),
        password=os.getenv("SMTP_PASSWORD", ""),
        to=os.getenv("EMAIL_TO", "") or os.getenv("SMTP_USER", ""),
    )


@dataclass(frozen=True)
class PortalCredentials:
    user: str
    password: str

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)


def get_portal_credentials(portal: str) -> PortalCredentials:
    """Login for a job portal from .env, e.g. NAUKRI_EMAIL / NAUKRI_PASSWORD."""
    prefix = portal.upper()
    return PortalCredentials(user=os.getenv(f"{prefix}_EMAIL", ""), password=os.getenv(f"{prefix}_PASSWORD", ""))


def setup_logging(level: int = logging.INFO) -> None:
    # Always log to stderr: stdout is reserved for the MCP stdio protocol.
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
