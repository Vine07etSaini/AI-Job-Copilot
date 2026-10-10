"""API tests: offline, against a temporary SQLite database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.deps import get_tracker
from backend.main import app
from job_copilot.db import Tracker
from job_copilot.models import JobPosting, JobStatus


@pytest.fixture
def tracker(tmp_path) -> Tracker:
    return Tracker(tmp_path / "tracker.db")


@pytest.fixture
def client(tracker: Tracker):
    app.dependency_overrides[get_tracker] = lambda: tracker
    yield TestClient(app)
    app.dependency_overrides.clear()


def _add(tracker: Tracker, ext: str, status: JobStatus, score: int = 80) -> str:
    posting = JobPosting(portal="sample", external_id=ext, title=f"Dev {ext}", company=f"Co {ext}", url="http://x")
    return tracker.upsert_job(posting, status, match_score=score)


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_list_filters_by_status(client, tracker):
    _add(tracker, "1", JobStatus.PENDING_REVIEW)
    _add(tracker, "2", JobStatus.APPLIED)
    rows = client.get("/api/jobs", params={"status": "pending_review"}).json()
    assert [r["title"] for r in rows] == ["Dev 1"]


def test_invalid_status_is_422(client):
    assert client.get("/api/jobs", params={"status": "nope"}).status_code == 422


def test_stats(client, tracker):
    _add(tracker, "1", JobStatus.PENDING_REVIEW)
    _add(tracker, "2", JobStatus.PENDING_REVIEW)
    body = client.get("/api/jobs/stats").json()
    assert body["total"] == 2 and body["by_status"] == {"pending_review": 2}


def test_approve_then_reject(client, tracker):
    jid = _add(tracker, "1", JobStatus.PENDING_REVIEW)
    assert client.post(f"/api/jobs/{jid}/approve").json()["status"] == "approved"
    # approved jobs cannot be approved again
    assert client.post(f"/api/jobs/{jid}/approve").status_code == 409
    assert client.post(f"/api/jobs/{jid}/reject", json={"reason": "meh"}).json()["status"] == "rejected"
    assert client.get(f"/api/jobs/{jid}").json()["status_reason"] == "meh"


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.post("/api/jobs/missing/approve").status_code == 404


def test_browser_status_inactive_without_browser(client):
    assert client.get("/api/browser/status").json() == {"active": False}


def test_browser_ws_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/browser/ws", headers={"origin": "http://evil.example"}):
            pass


def test_browser_ws_idle_without_browser(client):
    with client.websocket_connect("/api/browser/ws", headers={"origin": "http://localhost:5173"}) as ws:
        assert ws.receive_json() == {"type": "idle"}


def test_login_start_reports_state_and_hides_password(client, monkeypatch):
    from job_copilot.portals import naukri

    seen = {}

    async def fake_login(creds, finished, on_state, timeout=300):
        seen["creds"] = creds
        on_state("logged_in", "Logged in.")
        return True

    monkeypatch.setattr(naukri, "interactive_login", fake_login)
    body = {"email": "me@example.com", "password": "s3cret"}
    r = client.post("/api/login/naukri", json=body)
    assert r.status_code == 200
    assert "s3cret" not in r.text
    assert client.get("/api/login/naukri").json()["state"] in ("running", "logged_in")


def test_search_refused_while_login_running(client, monkeypatch):
    from backend.routers import login

    monkeypatch.setattr(login, "is_running", lambda: True)
    assert client.post("/api/search", json={"source": "sample", "limit": 1}).status_code == 409
