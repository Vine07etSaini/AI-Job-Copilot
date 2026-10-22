"""Log in to a job portal from the web UI: its login page opens in the live view; typed-in details are tried once."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, SecretStr

from backend import busy
from job_copilot.config import PortalCredentials, get_settings
from job_copilot.portals.base import BROWSER_PORTALS, get_portal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/login", tags=["login"])

# Per portal, so logging in to one portal never blocks a run on another. Module state: one local API process.
_states: dict[str, dict] = {}
_finished: dict[str, asyncio.Event] = {}
_tasks: dict[str, asyncio.Task] = {}


def is_running(portal: str | None = None) -> bool:
    """Is a login in progress on this portal (or, without a name, on any portal)?"""
    names = [portal] if portal else list(_tasks)
    return any(n in _tasks and not _tasks[n].done() for n in names)


class LoginRequest(BaseModel):
    """Both fields are optional: leave them out to type your details into the live view instead."""

    email: str | None = None
    password: SecretStr | None = None  # SecretStr keeps it out of reprs and logs; it is never stored


def _check(portal: str) -> None:
    if portal not in BROWSER_PORTALS:
        raise HTTPException(404, f"Unknown portal '{portal}'. Use one of: {', '.join(BROWSER_PORTALS)}.")


def _state(portal: str) -> dict:
    return _states.setdefault(portal, {"portal": portal, "state": "idle", "message": ""})


@router.get("/{portal}")
def status(portal: str) -> dict:
    _check(portal)
    return _state(portal)


@router.post("/{portal}")
async def start(portal: str, body: LoginRequest) -> dict:
    """Open the portal's login page in its own browser window (watch and take control in its live view)."""
    _check(portal)
    claimed = busy.acquire(portal)  # 409 if this portal is already searching, applying or logging in
    creds = None
    if body.email and body.password:
        creds = PortalCredentials(user=body.email, password=body.password.get_secret_value())
    from job_copilot.portals.browser import interactive_login

    site = get_portal(portal, get_settings().portals.get(portal))
    finished = _finished[portal] = asyncio.Event()
    state = _state(portal)

    def on_state(new: str, message: str) -> None:
        state.update(state=new, message=message)

    on_state("running", f"Opening {site.label}...")

    async def run() -> None:
        try:
            await interactive_login(site, creds, finished, on_state)
        except Exception as e:  # report to the UI rather than dying silently inside the task
            log.exception("Interactive login failed")
            on_state("failed", f"Login failed: {type(e).__name__}")
        finally:
            busy.release(claimed)

    _tasks[portal] = asyncio.create_task(run())
    return state


@router.post("/{portal}/done")
def done(portal: str) -> dict:
    """The user says they finished logging in by hand: stop waiting and verify."""
    _check(portal)
    if event := _finished.get(portal):
        event.set()
    return _state(portal)
