"""SQLite application tracker."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from job_copilot.config import DB_PATH, IST, ensure_dirs
from job_copilot.models import JobPosting, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    fingerprint      TEXT NOT NULL,
    portal           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    title            TEXT NOT NULL,
    company          TEXT NOT NULL,
    location         TEXT,
    url              TEXT NOT NULL,
    experience_text  TEXT,
    salary_text      TEXT,
    posted_at        TEXT,
    posted_text      TEXT,
    description      TEXT,
    tags_json        TEXT,
    recruiter_email  TEXT,
    recruiter_phone  TEXT,
    analysis_json    TEXT,
    match_score      INTEGER,
    match_json       TEXT,
    resume_path      TEXT,
    resume_url       TEXT,
    guard_json       TEXT,
    status           TEXT NOT NULL,
    status_reason    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    applied_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

JSON_COLUMNS = ("analysis_json", "match_json", "guard_json", "tags_json")

# Columns added after the first release: CREATE TABLE IF NOT EXISTS never adds one to an existing database.
ADDED_COLUMNS = {"posted_text": "TEXT", "tags_json": "TEXT"}


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def job_id(portal: str, external_id: str) -> str:
    return hashlib.sha1(f"{portal}:{external_id}".encode()).hexdigest()[:12]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def fingerprint(company: str, title: str) -> str:
    """Portal-independent key used to de-duplicate the same job across portals."""
    return hashlib.sha1(f"{_norm(company)}|{_norm(title)}".encode()).hexdigest()[:16]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an older database up to date; `CREATE TABLE IF NOT EXISTS` never adds a column to an existing table."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, decl in ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")  # names are constants, never user input


class Tracker:
    def __init__(self, path: Path = DB_PATH):
        ensure_dirs()
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            _add_missing_columns(c)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ writes

    def is_known(self, posting: JobPosting, forget_filtered_after_days: int | None = None) -> bool:
        """True when this posting, or the same company+title, is already in the tracker.

        With `forget_filtered_after_days`, a `filtered_out` row stops blocking once it is that old, so a job
        that was only skipped for its age can come back when it is re-posted. Every other status represents a
        decision already made (applied, rejected, waiting for review), so it blocks for good.

        Comparing `updated_at` as a string is sound only because every write goes through `now_iso()` at the
        one fixed IST offset.
        """
        sql = "SELECT 1 FROM jobs WHERE (id = ? OR fingerprint = ?)"
        args: list = [job_id(posting.portal, posting.external_id), fingerprint(posting.company, posting.title)]
        if forget_filtered_after_days:
            cutoff = datetime.now(IST) - timedelta(days=forget_filtered_after_days)
            sql += " AND (status != ? OR updated_at >= ?)"
            args += [str(JobStatus.FILTERED_OUT), cutoff.isoformat(timespec="seconds")]
        with self._conn() as c:
            return c.execute(sql + " LIMIT 1", args).fetchone() is not None

    def upsert_job(self, posting: JobPosting, status: JobStatus, reason: str = "", **fields: Any) -> str:
        jid = job_id(posting.portal, posting.external_id)
        ts = now_iso()
        row = {
            "id": jid,
            "fingerprint": fingerprint(posting.company, posting.title),
            "portal": posting.portal,
            "external_id": posting.external_id,
            "title": posting.title,
            "company": posting.company,
            "location": posting.location,
            "url": posting.url,
            "experience_text": posting.experience_text,
            "salary_text": posting.salary_text,
            "posted_at": posting.posted_at.isoformat() if posting.posted_at else None,
            "description": posting.description,
            "recruiter_email": posting.recruiter_email,
            "recruiter_phone": posting.recruiter_phone,
            "status": str(status),
            "status_reason": reason,
            "created_at": ts,
            "updated_at": ts,
            **{k: _encode(v) for k, v in fields.items()},
        }
        # Written only when present: ON CONFLICT copies every listed column, so an unconditional key would let
        # a later tag-less write (a re-score rebuilds the posting from the row) erase what the search found.
        if posting.posted_text:
            row["posted_text"] = posting.posted_text
        if posting.tags:
            row["tags_json"] = json.dumps(posting.tags)
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k not in ("id", "created_at"))
        with self._conn() as c:
            c.execute(
                f"INSERT INTO jobs ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
                list(row.values()),
            )
        return jid

    def update(self, jid: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [_encode(v) for v in fields.values()] + [jid])

    def delete(self, jid: str) -> bool:
        """Remove a job for good; it can come back as new in a later search. Returns whether it existed."""
        with self._conn() as c:
            return c.execute("DELETE FROM jobs WHERE id = ?", (jid,)).rowcount > 0

    def set_status(self, jid: str, status: JobStatus, reason: str = "") -> None:
        extra = {"applied_at": now_iso()} if status == JobStatus.APPLIED else {}
        self.update(jid, status=status, status_reason=reason, **extra)

    # ------------------------------------------------------------ reads

    def get(self, jid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return _decode(row) if row else None

    def list(self, status: str | None = None, since: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if since:
            sql += " AND updated_at >= ?"
            args.append(since)
        sql += " ORDER BY match_score IS NULL, match_score DESC, updated_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [_decode(r) for r in c.execute(sql, args).fetchall()]

    def applied_count_today(self, portal: str | None = None) -> int:
        today = datetime.now(IST).date().isoformat()
        sql = "SELECT COUNT(*) FROM jobs WHERE status = ? AND applied_at >= ?"
        args: list = [str(JobStatus.APPLIED), today]
        if portal:
            sql += " AND portal = ?"
            args.append(portal)
        with self._conn() as c:
            return c.execute(sql, args).fetchone()[0]


def _encode(v: Any) -> Any:
    if hasattr(v, "model_dump"):
        return json.dumps(v.model_dump(mode="json"))
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, JobStatus):
        return str(v)
    return v


def _decode(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in JSON_COLUMNS:
        raw = d.pop(k, None)
        d[k.removesuffix("_json")] = json.loads(raw) if raw else None
    return d
