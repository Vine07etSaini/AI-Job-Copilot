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
    with TestClient(app) as c:  # one event loop for the whole test, like the real server: background tasks survive
        yield c
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
    assert client.delete("/api/jobs/missing").status_code == 404


def test_delete_job_removes_row_and_resume_files(client, tracker, tmp_path, monkeypatch):
    from job_copilot.agents import resume_builder

    monkeypatch.setattr(resume_builder, "RESUMES_DIR", tmp_path)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    pdf.with_suffix(".json").write_text("{}")
    jid = _add(tracker, "1", JobStatus.PENDING_REVIEW)
    tracker.update(jid, resume_path=str(pdf))
    assert client.delete(f"/api/jobs/{jid}").json() == {"id": jid, "deleted": True}
    assert client.get(f"/api/jobs/{jid}").status_code == 404
    assert not pdf.exists() and not pdf.with_suffix(".json").exists()


def test_delete_keeps_applied_jobs_and_files_outside_the_resume_folder(client, tracker, tmp_path, monkeypatch):
    from job_copilot.agents import resume_builder

    monkeypatch.setattr(resume_builder, "RESUMES_DIR", tmp_path / "resumes")
    applied = _add(tracker, "1", JobStatus.APPLIED)
    assert client.delete(f"/api/jobs/{applied}").status_code == 409
    outside = tmp_path / "keep.pdf"
    outside.write_bytes(b"%PDF")
    jid = _add(tracker, "2", JobStatus.FILTERED_OUT)
    tracker.update(jid, resume_path=str(outside))
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert outside.exists()


def test_browser_status_inactive_without_browser(client):
    assert client.get("/api/browser/status").json() == {"active": False, "portals": [], "viewing": []}


def test_browser_ws_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/browser/ws?portal=naukri", headers={"origin": "http://evil.example"}):
            pass


def test_browser_ws_idle_without_browser(client):
    with client.websocket_connect("/api/browser/ws?portal=indeed", headers={"origin": "http://localhost:5173"}) as ws:
        assert ws.receive_json() == {"type": "idle"}


def test_login_start_reports_state_and_hides_password(client, monkeypatch):
    from job_copilot.portals import browser

    seen = {}

    async def fake_login(portal, creds, finished, on_state, timeout=300):
        seen["portal"], seen["creds"] = portal.name, creds
        on_state("logged_in", "Logged in.")
        return True

    monkeypatch.setattr(browser, "interactive_login", fake_login)
    body = {"email": "me@example.com", "password": "s3cret"}
    r = client.post("/api/login/naukri", json=body)
    assert r.status_code == 200
    assert "s3cret" not in r.text
    assert client.get("/api/login/naukri").json()["state"] in ("running", "logged_in")


def test_login_works_per_portal(client, monkeypatch):
    from job_copilot.portals import browser

    async def fake_login(portal, creds, finished, on_state, timeout=300):
        on_state("logged_in", f"{portal.label} ok")
        return True

    monkeypatch.setattr(browser, "interactive_login", fake_login)
    naukri_before = client.get("/api/login/naukri").json()
    assert client.post("/api/login/indeed", json={}).json()["portal"] == "indeed"
    assert client.get("/api/login/naukri").json() == naukri_before  # the indeed login leaves naukri's alone
    assert client.get("/api/login/monster").status_code == 404


def test_same_portal_cannot_run_twice_but_others_can(client, monkeypatch):
    from backend import busy
    from job_copilot import pipeline

    async def fake_discover(source, limit=None, rescore_first=True):
        return {"found": 0}

    monkeypatch.setattr(pipeline, "discover", fake_discover)
    claimed = busy.acquire("naukri")  # e.g. a Naukri login or search is running
    try:
        r = client.post("/api/search", json={"source": "naukri", "limit": 1})
        assert r.status_code == 409 and "naukri is busy" in r.json()["detail"]
        assert client.post("/api/search", json={"source": "indeed", "limit": 1}).json() == {"found": 0}
        assert client.post("/api/login/naukri", json={}).status_code == 409
    finally:
        busy.release(claimed)
    assert client.post("/api/search", json={"source": "naukri", "limit": 1}).status_code == 200


class _FakeSite:
    """Stands in for a browser portal: registers a page in the live view like ChromePortal does."""

    def __init__(self, name):
        self.name, self.shown = name, []

    async def __aenter__(self):
        from job_copilot.portals import live_view

        class Page:
            def is_closed(self):
                return False

        live_view.set_page(self.name, Page())
        return self

    async def __aexit__(self, *exc):
        from job_copilot.portals import live_view

        live_view.set_page(self.name, None)

    async def show(self, url):
        self.shown.append(url)


def test_open_posting_in_live_view_then_close(client, tracker, monkeypatch):
    import time

    from backend.routers import browser

    sites = {}
    monkeypatch.setattr(browser, "get_portal", lambda name, settings=None: sites.setdefault(name, _FakeSite(name)))
    jid = tracker.upsert_job(JobPosting(portal="indeed", external_id="9", title="Dev", company="X",
                                        url="https://in.indeed.com/viewjob?jk=9"), JobStatus.PENDING_REVIEW)
    assert client.post("/api/browser/open", json={"job_id": jid}).json()["portal"] == "indeed"
    assert sites["indeed"].shown == ["https://in.indeed.com/viewjob?jk=9"]
    status = client.get("/api/browser/status").json()
    assert status["viewing"] == ["indeed"] and "indeed" in status["portals"]
    # the portal is busy while the posting is open: no second session on it
    assert client.post("/api/browser/open", json={"job_id": jid}).status_code == 409
    assert client.post("/api/browser/indeed/close").json()["closing"] is True
    for _ in range(50):  # the viewing task ends on the next loop turn
        if not client.get("/api/browser/status").json()["viewing"]:
            break
        time.sleep(0.05)
    assert client.get("/api/browser/status").json() == {"active": False, "portals": [], "viewing": []}


def test_open_posting_rejects_unknown_jobs_and_offline_portals(client, tracker):
    assert client.post("/api/browser/open", json={"job_id": "missing"}).status_code == 404
    jid = _add(tracker, "1", JobStatus.PENDING_REVIEW)  # a 'sample' job: no browser window
    assert client.post("/api/browser/open", json={"job_id": jid}).status_code == 400


def test_open_in_chrome_uses_the_copilot_chrome_for_any_status(client, tracker, monkeypatch):
    from job_copilot.portals import browser as browser_portal

    opened = []

    async def fake_open(url, cdp_url):
        opened.append((url, cdp_url))

    monkeypatch.setattr(browser_portal, "open_in_chrome", fake_open)
    jid = tracker.upsert_job(JobPosting(portal="naukri", external_id="7", title="Dev", company="X",
                                        url="https://www.naukri.com/job-7"), JobStatus.REJECTED)
    assert client.post("/api/browser/open-chrome", json={"job_id": jid}).json()["portal"] == "naukri"
    assert opened == [("https://www.naukri.com/job-7", "http://127.0.0.1:9222")]
    assert client.post("/api/browser/open-chrome", json={"job_id": _add(tracker, "s", JobStatus.REJECTED)}
                       ).status_code == 400  # sample jobs: plain link
