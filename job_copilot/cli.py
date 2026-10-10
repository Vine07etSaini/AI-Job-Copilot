"""Command-line interface:  python main.py --help"""

from __future__ import annotations

import asyncio
import json
import os
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from job_copilot.config import setup_logging

app = typer.Typer(add_completion=False, no_args_is_help=True, help="AI Job Copilot - Phase 1 MVP")
console = Console()


@app.callback()
def _init(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    import logging

    setup_logging(logging.DEBUG if verbose else logging.INFO)


@app.command()
def check() -> None:
    """Validate config, profile, Gemini API key and email settings."""
    from job_copilot.config import get_email_config, get_profile, get_settings
    from job_copilot.llm.gemini import Gemini, gemini_available, model_name

    profile, settings = get_profile(), get_settings()
    console.print(f"[green]OK[/] profile: {profile.personal.name}, {len(profile.skills)} skills, "
                  f"{len(profile.projects)} projects")
    console.print(f"[green]OK[/] settings: mode={settings.mode}, threshold={settings.match_threshold}, "
                  f"max_age={settings.max_job_age_days}d")
    if not gemini_available():
        console.print("[red]MISSING[/] GEMINI_API_KEY in .env (LLM features will use offline heuristics)")
    else:
        try:
            reply = Gemini().generate_text("Reply with the single word: ready")
            console.print(f"[green]OK[/] Gemini {model_name()}: {reply.strip()[:40]}")
        except Exception as e:
            console.print(f"[red]FAIL[/] Gemini {model_name()}: {e}")
    _check_groq()
    _check_ollama()
    _check_active_llm()
    email = get_email_config()
    console.print(f"[green]OK[/] email -> {email.to}" if email.configured
                  else "[yellow]SKIP[/] email not configured (reports saved locally only)")


def _check_active_llm() -> None:
    from job_copilot.llm.factory import get_llm, provider_name

    try:
        llm = get_llm()
    except ValueError as e:
        console.print(f"[red]FAIL[/] {e}")
        return
    label = getattr(llm, "label", type(llm).__name__) if llm else "none (offline heuristics)"
    console.print(f"[green]OK[/] LLM in use: {label} (LLM_PROVIDER={provider_name()})")


def _check_groq() -> None:
    from job_copilot.llm.providers import groq_available, groq_model, groq_models

    if not groq_available():
        console.print("[yellow]SKIP[/] Groq: GROQ_API_KEY not set (optional)")
        return
    try:
        models, wanted = groq_models(), groq_model()
    except Exception as e:
        console.print(f"[red]FAIL[/] Groq: {e}")
        return
    note = "" if wanted in models else f" [yellow](GROQ_MODEL '{wanted}' is not in your list)[/]"
    console.print(f"[green]OK[/] Groq key valid, {len(models)} models available, using {wanted}{note}")


def _check_ollama() -> None:
    import httpx

    from job_copilot.llm.providers import ollama_host, ollama_model, ollama_models

    try:
        models = ollama_models()
    except httpx.ConnectError:
        console.print(f"[yellow]SKIP[/] Ollama not running at {ollama_host()} (start it with: ollama serve)")
        return
    except Exception as e:
        console.print(f"[red]FAIL[/] Ollama at {ollama_host()}: {e}")
        return
    console.print(f"[green]OK[/] Ollama at {ollama_host()}: {len(models)} model(s) downloaded")
    for m in models:
        console.print(f"      {m.name}  ({m.size_gb} GB)")
    if (wanted := ollama_model()) and wanted not in {m.name for m in models}:
        console.print(f"[yellow]WARN[/] OLLAMA_MODEL '{wanted}' is not downloaded (ollama pull {wanted})")


@app.command()
def run(source: str = typer.Option("naukri", help="naukri | sample"),
        limit: int = typer.Option(None, help="Max job pages to open"),
        email: bool = typer.Option(True, help="Email the daily report")) -> None:
    """Daily run: discover, filter, tailor resumes, (auto-apply), report."""
    from job_copilot import pipeline

    result = asyncio.run(pipeline.run_daily(source, limit, send_report=email))
    console.print_json(json.dumps(result["discovery"]))
    if result["apply"]:
        console.print_json(json.dumps(result["apply"]))
    console.print(result["report"]["summary"])
    console.print(f"Report: {result['report']['report_path']}")


@app.command()
def jobs(status: str = typer.Option(None, help="Filter by status"), limit: int = 30) -> None:
    """List tracked jobs."""
    from job_copilot.db import Tracker

    t = Table("id", "score", "status", "title", "company", "portal", "why")
    for j in Tracker().list(status=status, limit=limit):
        why = (j.get("match") or {}).get("explanation") or j["status_reason"] or ""
        t.add_row(j["id"], str(j["match_score"] or "-"), j["status"], j["title"], j["company"], j["portal"], why)
    console.print(t)


@app.command()
def review() -> None:
    """Review pending jobs one by one: approve, reject, skip or open the resume."""
    from job_copilot.db import Tracker
    from job_copilot.models import JobStatus

    tracker = Tracker()
    pending = tracker.list(status=str(JobStatus.PENDING_REVIEW))
    if not pending:
        console.print("Nothing to review.")
        return
    for j in pending:
        console.rule(f"[bold]{j['title']}[/] @ {j['company']}  ({j['portal']}, score {j['match_score']})")
        console.print((j.get("match") or {}).get("explanation", ""))
        console.print(f"Job: {j['url']}\nResume: {j['resume_path']}")
        if (g := j.get("guard")) and g.get("violations"):
            console.print(f"[yellow]Honesty Guard fixed:[/] {'; '.join(g['violations'])}")
        while True:
            choice = typer.prompt("[a]pprove / [r]eject / [s]kip / [o]pen resume / [j]ob page / [q]uit",
                                  default="s").lower()[:1]
            if choice == "o" and j["resume_path"]:
                os.startfile(j["resume_path"]) if os.name == "nt" else webbrowser.open(Path(j["resume_path"]).as_uri())
            elif choice == "j":
                webbrowser.open(j["url"])
            elif choice == "a":
                tracker.set_status(j["id"], JobStatus.APPROVED, "approved by user")
                console.print("[green]approved[/]")
                break
            elif choice == "r":
                tracker.set_status(j["id"], JobStatus.REJECTED, typer.prompt("reason", default="not interested"))
                console.print("[red]rejected[/]")
                break
            elif choice == "q":
                return
            else:
                break
    console.print("Apply to approved jobs with:  python main.py apply")


@app.command()
def apply(max_count: int = typer.Option(None, "--max", help="Max applications this run")) -> None:
    """Apply to approved jobs (respects daily and per-portal caps)."""
    from job_copilot import pipeline

    console.print_json(json.dumps(asyncio.run(pipeline.apply_approved(max_count))))


@app.command()
def report(send: bool = typer.Option(False, help="Email the report")) -> None:
    """Build today's Excel report."""
    from job_copilot import pipeline

    r = pipeline.daily_report(send=send)
    console.print(r["summary"])
    console.print(f"Report: {r['report_path']}  (emailed: {r['emailed']})")


@app.command()
def login(portal: str = typer.Argument("naukri")) -> None:
    """Log in to a portal (automatically with .env credentials, else by hand); the session is reused later."""
    if portal != "naukri":
        raise typer.BadParameter("Only 'naukri' is supported in Phase 1")
    from job_copilot.portals.naukri import login_session

    how = asyncio.run(login_session())
    console.print(f"Session saved ({how}).")


@app.command()
def chrome() -> None:
    """Start the debug Chrome the Naukri portal attaches to (log in to Naukri in it once)."""
    from job_copilot.config import get_settings
    from job_copilot.portals.chrome_debug import ensure_chrome

    url = get_settings().portals["naukri"].cdp_url
    started = ensure_chrome(url)
    console.print(f"Chrome {'started' if started else 'already running'} on {url}.")


@app.command("import-resume")
def import_resume(
    pdf: Path = typer.Argument(None, help="Path to your resume PDF (omit together with --apply)"),
    apply: bool = typer.Option(False, "--apply", help="Promote the reviewed draft to config/profile.yaml"),
) -> None:
    """Build config/profile.imported.yaml from a resume PDF; edit it, then run `import-resume --apply`."""
    from job_copilot.config import PROFILE_DRAFT_PATH

    if apply:
        _apply_draft(PROFILE_DRAFT_PATH)
        return
    if pdf is None:
        raise typer.BadParameter("Give the resume PDF, e.g.: python main.py import-resume resume.pdf")

    from job_copilot.agents import profile_importer as pi
    from job_copilot.config import get_profile, save_profile
    from job_copilot.llm.factory import get_llm

    if (llm := get_llm()) is None:
        raise typer.BadParameter("No LLM configured. Set GEMINI_API_KEY, GROQ_API_KEY or OLLAMA_MODEL in .env "
                                 "(importing needs the LLM).")
    try:
        text = pi.extract_pdf_text(pdf)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    extract, dropped = pi.verify_skills(pi.extract_profile(text, llm), text)
    profile = pi.merge_into_profile(extract, get_profile())

    t = Table("section", "found")
    for name, found in [("skills", len(profile.skills)), ("projects", len(profile.projects)),
                        ("experience", len(profile.experience)), ("education", len(profile.education))]:
        t.add_row(name, str(found))
    console.print(t)
    if dropped:
        console.print(f"[yellow]Dropped (not in the PDF text):[/] {', '.join(dropped)}")
    if missing := pi.empty_fields(profile):
        console.print(f"[yellow]Not found in the resume, fill in by hand:[/] {', '.join(missing)}")
    console.print("[dim]Kept from your current profile: preferences, equivalents, learning_goals. "
                  "Skill years are left empty.[/]")
    save_profile(profile, PROFILE_DRAFT_PATH)
    console.print(f"Draft written to {PROFILE_DRAFT_PATH}. Edit it, then run: python main.py import-resume --apply")


def _apply_draft(draft: Path) -> None:
    import shutil

    from job_copilot.config import PROFILE_PATH, get_profile

    if not draft.is_file():
        raise typer.BadParameter(f"No draft at {draft}. Run: python main.py import-resume resume.pdf")
    get_profile(draft)  # fail now, not on the next run, if the edited draft is invalid
    backup = PROFILE_PATH.with_suffix(".yaml.bak")
    shutil.copy2(PROFILE_PATH, backup)
    shutil.copy2(draft, PROFILE_PATH)
    draft.unlink()
    console.print(f"[green]profile.yaml updated[/] (previous version saved as {backup.name}).")
    console.print("Next: python main.py check")


@app.command()
def chat() -> None:
    """Chat with the Gemini agent (it uses the MCP tools)."""
    from job_copilot.gemini_agent import chat as run_chat

    asyncio.run(run_chat())


@app.command()
def ask(question: str) -> None:
    """Ask the Gemini agent a single question, e.g. ask "show my pending jobs"."""
    from rich.markdown import Markdown

    from job_copilot.gemini_agent import ask as run_ask

    console.print(Markdown(asyncio.run(run_ask(question))))


@app.command("mcp")
def mcp_serve() -> None:
    """Run the MCP server over stdio (for Claude Desktop, Claude Code, Cursor, ...)."""
    from job_copilot.mcp_server import server

    server.run()
