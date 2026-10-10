"""Log in to Naukri from the web UI: opens the login page in the live view; optionally tries typed-in details once."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, SecretStr

from job_copilot.config import PortalCredentials
from job_copilot.portals import live_view

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/login", tags=["login"])

# One login at a time; module state because the API is a single local process.
_state: dict = {"state": "idle", "message": ""}
_finished = asyncio.Event()
_task: asyncio.Task | None = None


def is_running() -> bool:
    return _task is not None and not _task.done()


class LoginRequest(BaseModel):
    """Both fields are optional: leave them out to type your details into the live view instead."""

    email: str | None = None
    password: SecretStr | None = None  # SecretStr keeps it out of reprs and logs; it is never stored


def _set(state: str, message: str) -> None:
    _state.update(state=state, message=message)


@router.get("/naukri")
def status() -> dict:
    return _state


@router.post("/naukri")
async def start(body: LoginRequest) -> dict:
    """Open Naukri's login page in the automated browser (watch it in the live view)."""
    global _task
    if is_running() or live_view.is_active():
        raise HTTPException(409, "The browser is busy (a login or a search/apply run is in progress).")
    creds = None
    if body.email and body.password:
        creds = PortalCredentials(user=body.email, password=body.password.get_secret_value())
    from job_copilot.portals.naukri import interactive_login

    _finished.clear()
    _set("running", "Opening Naukri...")

    async def run() -> None:
        try:
            await interactive_login(creds, _finished, _set)
        except Exception as e:  # report to the UI rather than dying silently inside the task
            log.exception("Interactive login failed")
            _set("failed", f"Login failed: {type(e).__name__}")

    _task = asyncio.create_task(run())
    return _state


@router.post("/naukri/done")
def done() -> dict:
    """The user says they finished logging in by hand: stop waiting and verify."""
    _finished.set()
    return _state
