"""FastAPI app. Run from the repo root with `python -m backend.run`."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routers import actions, browser, jobs, login, profile
from job_copilot.config import setup_logging

setup_logging()

app = FastAPI(title="AI Job Copilot API", version="0.1.0")

# The Vite dev server proxies /api, but allow it directly too (e.g. when calling from another origin).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs.router)
app.include_router(actions.router)
app.include_router(profile.router)
app.include_router(browser.router)
app.include_router(login.router)


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
