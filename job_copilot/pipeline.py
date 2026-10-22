"""Daily run as LangGraph graphs: discover -> freshness -> de-dupe -> analyze -> score -> tailor -> (review | apply) -> report.

Four graphs, each behind a plain async function so the CLI, MCP server and API are unchanged:
  POSTING_GRAPH   analyze -> score -> (filter_out | approve_base (hybrid mode) | tailor -> render -> save)
  DISCOVER_GRAPH  rescore (once per run) -> search (cards ranked best-first) -> triage (optional LLM) -> pick -> fetch
                  -> process (runs POSTING_GRAPH) -> pick ... until done; discover_many runs portals in parallel
  APPLY_GRAPH     plan (splits the daily cap between portals) -> apply_all (one session per portal, in parallel)
  DAILY_GRAPH     discover -> (apply, auto/hybrid mode only) -> report
Per-run objects that are not part of the state (tracker, llm, portal, settings) travel in `config["configurable"]`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from job_copilot.agents import emailer, reporter
from job_copilot.agents.card_triage import triage_cards
from job_copilot.agents.freshness import is_fresh
from job_copilot.agents.honesty_guard import GuardReport
from job_copilot.agents.jd_analyzer import analyze_jd
from job_copilot.agents.matcher import blacklisted_company, excluded_title_word, prescore, score_job
from job_copilot.agents.resume_builder import build_resume, resume_gaps, tailor
from job_copilot.agents.salary import meets_salary_floor
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import IST, get_profile, get_settings
from job_copilot.db import Tracker, job_id
from job_copilot.llm.chat import LLM
from job_copilot.llm.factory import get_llm
from job_copilot.models import JDAnalysis, JobPosting, JobStatus, MatchResult, TailoredContent
from job_copilot.portals.base import BROWSER_PORTALS, PortalBlocked, get_portal

log = logging.getLogger(__name__)

# The discover loop takes ~4 graph steps per job; LangGraph's default limit of 25 would stop it after a few jobs.
DISCOVER_RECURSION_LIMIT = 2000
SCORE_REASON = "score "  # start of the status reason _filter_out writes; only these jobs are worth re-scoring
RESCORE_LIMIT = 500
AUTO_APPLY_MODES = ("auto", "hybrid")  # modes in which the daily run applies by itself
# One re-score at a time: two portals searching in parallel must not tailor the same job twice.
_rescore_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # event loop -> asyncio.Lock


def _llm() -> LLM | None:
    return get_llm()


def _deps(config: RunnableConfig) -> dict[str, Any]:
    return config["configurable"]


# ---------------------------------------------------------------- one posting

class PostingState(TypedDict, total=False):
    posting: JobPosting
    jid: str
    analysis: JDAnalysis
    match: MatchResult
    gaps: list[str]     # matched must-have skills the base resume doesn't show
    content: TailoredContent
    guard_report: GuardReport
    pdf: Path
    url: str
    status: JobStatus


def _analyze(state: PostingState, config: RunnableConfig) -> PostingState:
    d, posting = _deps(config), state["posting"]
    # A stored analysis (re-scoring a known job) is reused: the JD hasn't changed, so no new LLM call.
    analysis = state.get("analysis") or analyze_jd(posting, d["bank"], d["llm"])
    posting.recruiter_email = posting.recruiter_email or analysis.recruiter_email
    posting.recruiter_phone = posting.recruiter_phone or analysis.recruiter_phone
    return {"analysis": analysis, "jid": job_id(posting.portal, posting.external_id)}


def _score(state: PostingState, config: RunnableConfig) -> PostingState:
    d, posting = _deps(config), state["posting"]
    match = score_job(d["profile"], state["analysis"], d["bank"], d["settings"].score_weights, posting)
    log.info("%s @ %s -> %s", posting.title, posting.company, match.explanation)
    return {"match": match, "gaps": resume_gaps(d["profile"], match, d["bank"])}


def _route_score(state: PostingState, config: RunnableConfig) -> str:
    settings = _deps(config)["settings"]
    if state["match"].score < settings.match_threshold:
        return "filter_out"
    # Hybrid: a job your base resume already covers needs no new resume, so it skips tailoring and review.
    return "approve_base" if settings.mode == "hybrid" and not state["gaps"] else "tailor"


def _approve_base(state: PostingState, config: RunnableConfig) -> PostingState:
    d, match = _deps(config), state["match"]
    d["tracker"].upsert_job(state["posting"], JobStatus.APPROVED,
                            "base resume already shows the key skills: auto-apply with your Naukri resume",
                            analysis_json=state["analysis"], match_score=match.score, match_json=match)
    return {"status": JobStatus.APPROVED}


def _filter_out(state: PostingState, config: RunnableConfig) -> PostingState:
    d, match = _deps(config), state["match"]
    d["tracker"].upsert_job(state["posting"], JobStatus.FILTERED_OUT,
                            f"score {match.score} < {d['settings'].match_threshold}",
                            analysis_json=state["analysis"], match_score=match.score, match_json=match)
    return {"status": JobStatus.FILTERED_OUT}


def _tailor(state: PostingState, config: RunnableConfig) -> PostingState:
    d = _deps(config)
    content, guard_report = tailor(d["profile"], state["posting"], state["analysis"], state["match"], d["bank"],
                                   d["llm"], d["settings"].max_keyword_repeats)
    if guard_report.violations:
        log.warning("Honesty Guard fixed %d issue(s): %s", len(guard_report.violations), guard_report.violations)
    return {"content": content, "guard_report": guard_report}


async def _render(state: PostingState, config: RunnableConfig) -> PostingState:
    pdf, url = await build_resume(_deps(config)["profile"], state["posting"], state["content"], state["jid"],
                                  state["analysis"], state["guard_report"])
    return {"pdf": pdf, "url": url}


def _save(state: PostingState, config: RunnableConfig) -> PostingState:
    d, posting, match = _deps(config), state["posting"], state["match"]
    mode = d["settings"].mode
    status = JobStatus.APPROVED if mode == "auto" else JobStatus.PENDING_REVIEW
    if status == JobStatus.APPROVED:
        reason = "auto mode"
    elif mode == "hybrid" and state["gaps"]:
        reason = f"new resume needed: your resume doesn't show {', '.join(state['gaps'])}"
    else:
        reason = "awaiting your review"
    d["tracker"].upsert_job(posting, status, reason,
                            analysis_json=state["analysis"], match_score=match.score, match_json=match,
                            resume_path=str(state["pdf"]), resume_url=state["url"], guard_json=state["guard_report"])
    if d["settings"].email_each_resume:
        emailer.send_email(f"Tailored resume: {posting.title} @ {posting.company}",
                           f"{match.explanation}\n\n{posting.url}", [state["pdf"]])
    return {"status": status}


def _build_posting_graph():
    g = StateGraph(PostingState)
    g.add_node("analyze", _analyze)
    g.add_node("score", _score)
    g.add_node("filter_out", _filter_out)
    g.add_node("approve_base", _approve_base)
    g.add_node("tailor", _tailor)
    g.add_node("render", _render)
    g.add_node("save", _save)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "score")
    g.add_conditional_edges("score", _route_score, ["filter_out", "approve_base", "tailor"])
    g.add_edge("tailor", "render")
    g.add_edge("render", "save")
    g.add_edge("filter_out", END)
    g.add_edge("approve_base", END)
    g.add_edge("save", END)
    return g.compile()


POSTING_GRAPH = _build_posting_graph()


async def process_posting(posting: JobPosting, tracker: Tracker, llm: LLM | None,
                          analysis: JDAnalysis | None = None) -> tuple[str, JobStatus]:
    """Analyze (unless `analysis` is given), score and, if good enough, tailor a resume for a posting."""
    profile = get_profile()
    config = {"configurable": {"tracker": tracker, "llm": llm, "profile": profile, "bank": SkillBank(profile),
                               "settings": get_settings()}}
    state = {"posting": posting, **({"analysis": analysis} if analysis else {})}
    final = await POSTING_GRAPH.ainvoke(state, config=config)
    return final["jid"], final["status"]


# ---------------------------------------------------------------- discovery

class DiscoverState(TypedDict, total=False):
    cards: list[JobPosting]
    index: int          # next card to look at
    processed: int      # job pages fully processed, counted against the limit
    current: JobPosting | None
    posting: JobPosting | None
    blocked: bool
    stats: dict[str, int]


def _row_posting(row: dict) -> JobPosting:
    """Rebuild a posting from a tracker row (the search-card snippet is the one field not stored)."""
    return JobPosting(
        portal=row["portal"], external_id=row["external_id"], title=row["title"], company=row["company"],
        url=row["url"], location=row["location"] or "", experience_text=row["experience_text"] or "",
        salary_text=row["salary_text"] or "", description=row["description"] or "",
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        posted_text=row["posted_text"] or "", tags=row["tags"] or [],
        recruiter_email=row["recruiter_email"], recruiter_phone=row["recruiter_phone"],
    )


async def _rescore_filtered(tracker: Tracker, llm: LLM | None, settings) -> int:
    """Score jobs that missed the threshold again with the current profile; tailor those that now pass.

    Scoring is local (the stored JD analysis is reused), so this costs no page loads; only jobs that now pass
    make LLM calls, for tailoring. Returns how many now pass.
    """
    lock = _rescore_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        return await _rescore_unlocked(tracker, llm, settings)


async def _rescore_unlocked(tracker: Tracker, llm: LLM | None, settings) -> int:
    profile = get_profile()
    bank, passed = SkillBank(profile), 0
    for row in tracker.list(status=str(JobStatus.FILTERED_OUT), limit=RESCORE_LIMIT):
        if not (row["status_reason"] or "").startswith(SCORE_REASON) or not row["analysis"]:
            continue
        posting = _row_posting(row)
        fresh = is_fresh(posting.posted_at, settings.max_job_age_days)
        if fresh is None and (ps := settings.portals.get(posting.portal)) and ps.trust_search_age_filter:
            # undated, but it was recent when the search found it: count its age from then
            fresh = is_fresh(datetime.fromisoformat(row["created_at"]), settings.max_job_age_days)
        if fresh is False or (fresh is None and settings.skip_if_date_unknown):
            continue
        if excluded_title_word(posting.title, settings.exclude_title_words):
            continue
        analysis = JDAnalysis.model_validate(row["analysis"])
        match = score_job(profile, analysis, bank, settings.score_weights, posting)
        if match.score < settings.match_threshold:
            if match.score != row["match_score"]:
                tracker.update(row["id"], match_score=match.score, match_json=match,
                               status_reason=f"{SCORE_REASON}{match.score} < {settings.match_threshold}")
            continue
        try:
            _, status = await process_posting(posting, tracker, llm, analysis)
        except Exception:
            log.exception("Failed to re-process %s", posting.url)
            continue
        log.info("Re-scored %s @ %s -> %s", posting.title, posting.company, status)
        passed += 1
    return passed


async def _rescore(state: DiscoverState, config: RunnableConfig) -> DiscoverState:
    d = _deps(config)
    if not d.get("rescore", True):  # a parallel run re-scores once before the portals start
        return {}
    stats = dict(state["stats"])
    stats["rescored"] = await _rescore_filtered(d["tracker"], d["llm"], d["settings"])
    return {"stats": stats}


async def _search(state: DiscoverState, config: RunnableConfig) -> DiscoverState:
    d = _deps(config)
    settings, profile, portal = d["settings"], d["profile"], d["portal"]
    stats = dict(state["stats"])
    seen: set[str] = set()
    cards: list[JobPosting] = []
    queries = [(r, loc) for r in profile.preferences.roles for loc in (profile.preferences.locations or [""])]
    for i, (role, location) in enumerate(queries):
        # Share what is still needed over the queries left, so a productive first query shortens the rest.
        # Floored at 1 rather than skipped: skipping a satisfied query would bias the run to the first role.
        need = max(0, settings.search_card_target - len(cards))
        share = max(1, math.ceil(need / (len(queries) - i)))
        try:
            found = await portal.search(role, location, settings.max_job_age_days,
                                        profile.total_experience_years, share)
        except PortalBlocked as e:
            log.error("%s", e)
            stats["errors"] += 1
            continue
        for c in found:
            if c.external_id not in seen:
                seen.add(c.external_id)
                cards.append(c)
    stats["found"] = len(cards)
    # Best-first from the title plus the card's tags or snippet, so the `max_jobs_per_run` page loads go to the
    # likeliest matches (the sort is stable, so the portal's own order breaks ties). `_triage` may re-rank this.
    bank = SkillBank(profile)
    cards.sort(key=lambda c: prescore(c, profile, bank), reverse=True)
    return {"cards": cards, "index": 0, "processed": 0, "stats": stats}


def _triage(state: DiscoverState, config: RunnableConfig) -> DiscoverState:
    """Let the LLM re-read the search cards before any page is opened.

    Sync on purpose: `generate_json` is blocking I/O, and LangGraph runs sync nodes on a worker thread,
    so this call cannot stall the other portals running in parallel on the event loop.
    """
    d = _deps(config)
    settings, profile = d["settings"], d["profile"]
    if settings.triage == "off" or d["llm"] is None or not state["cards"]:
        return {}
    cards, report = triage_cards(state["cards"], profile, SkillBank(profile), d["llm"], settings.triage,
                                 settings.triage_threshold, settings.triage_max_cards)
    log.info("%s triage: scored %d of %d cards, dropped %d", d["portal"].name, report.scored,
             len(state["cards"]), report.dropped)
    for line in report.lines:
        log.debug("triage %s", line)
    for note in report.notes:
        log.warning("Card triage: %s", note)
    stats = dict(state["stats"])
    stats["triage_scored"] += report.scored
    stats["triage_dropped"] += report.dropped
    return {"cards": cards, "index": 0, "stats": stats}


def _pick(state: DiscoverState, config: RunnableConfig) -> DiscoverState:
    """Advance to the next card that survives the blacklist, de-dupe, title, salary and freshness checks."""
    d = _deps(config)
    settings, tracker, floor = d["settings"], d["tracker"], d["profile"].preferences.min_salary_lpa
    stats, index, cards = dict(state["stats"]), state["index"], state["cards"]
    while index < len(cards) and state["processed"] < d["limit"]:
        card, index = cards[index], index + 1
        if entry := blacklisted_company(card.company, settings.blacklist_companies):
            stats["blacklisted"] += 1
            log.info("Skipping %s @ %s: blacklisted as '%s'", card.title, card.company, entry)
        elif tracker.is_known(card, settings.forget_filtered_after_days):
            stats["duplicate"] += 1
        elif word := excluded_title_word(card.title, settings.exclude_title_words):
            stats["excluded_title"] += 1
            tracker.upsert_job(card, JobStatus.FILTERED_OUT, f"title has '{word}'")
        elif not meets_salary_floor(card.salary_text, floor):
            stats["low_salary"] += 1
            tracker.upsert_job(card, JobStatus.FILTERED_OUT, f"salary '{card.salary_text}' below {floor:g} LPA")
        elif _fresh_enough(card, tracker, stats, settings, d["portal"]):  # freshness check 1: search card date
            return {"current": card, "index": index, "stats": stats}
    return {"current": None, "index": index, "stats": stats}


def _route_pick(state: DiscoverState) -> str:
    return END if state["current"] is None else "fetch"


async def _fetch(state: DiscoverState, config: RunnableConfig) -> DiscoverState:
    d, card = _deps(config), state["current"]
    stats = dict(state["stats"])
    try:
        posting = await d["portal"].fetch_details(card)
        # freshness check 2: the job page's own posted date
        if not _fresh_enough(posting, d["tracker"], stats, d["settings"], d["portal"]):
            posting = None
    except PortalBlocked as e:
        log.error("%s", e)
        stats["errors"] += 1
        return {"posting": None, "blocked": True, "stats": stats}
    except Exception:
        log.exception("Failed to process %s", card.url)
        stats["errors"] += 1
        posting = None
    return {"posting": posting, "stats": stats}


def _route_fetch(state: DiscoverState) -> str:
    if state.get("blocked"):
        return END
    return "pick" if state["posting"] is None else "process"


async def _process(state: DiscoverState, config: RunnableConfig) -> DiscoverState:
    d, posting = _deps(config), state["posting"]
    stats = dict(state["stats"])
    try:
        _, status = await process_posting(posting, d["tracker"], d["llm"])
        stats[str(status)] += 1
    except Exception:
        log.exception("Failed to process %s", posting.url)
        stats["errors"] += 1
    return {"processed": state["processed"] + 1, "stats": stats}


def _build_discover_graph():
    g = StateGraph(DiscoverState)
    g.add_node("rescore", _rescore)
    g.add_node("search", _search)
    g.add_node("triage", _triage)
    g.add_node("pick", _pick)
    g.add_node("fetch", _fetch)
    g.add_node("process", _process)
    g.add_edge(START, "rescore")
    g.add_edge("rescore", "search")
    g.add_edge("search", "triage")
    g.add_edge("triage", "pick")
    g.add_conditional_edges("pick", _route_pick, ["fetch", END])
    g.add_conditional_edges("fetch", _route_fetch, ["pick", "process", END])
    g.add_edge("process", "pick")
    return g.compile()


DISCOVER_GRAPH = _build_discover_graph()


async def discover(source: str = "naukri", limit: int | None = None, rescore_first: bool = True) -> dict:
    """Search the portal, filter and process new jobs. Returns counts."""
    settings, profile = get_settings(), get_profile()
    stats = {"rescored": 0, "found": 0, "triage_scored": 0, "triage_dropped": 0, "stale": 0, "undated": 0,
             "duplicate": 0, "blacklisted": 0, "excluded_title": 0, "low_salary": 0, "filtered_out": 0,
             "pending_review": 0, "approved": 0, "errors": 0}
    async with get_portal(source, settings.portals.get(source)) as portal:
        config = {"recursion_limit": DISCOVER_RECURSION_LIMIT,
                  "configurable": {"tracker": Tracker(), "llm": _llm(), "portal": portal, "profile": profile,
                                   "settings": settings, "limit": limit or settings.max_jobs_per_run,
                                   "rescore": rescore_first}}
        final = await DISCOVER_GRAPH.ainvoke({"stats": stats}, config=config)
    return final["stats"]


async def discover_many(sources: list[str], limit: int | None = None) -> dict:
    """Search several portals at the same time, each in its own Chrome window and live view.

    Re-scoring runs once first. One portal failing (blocked, Chrome error) does not stop the others; its entry
    then holds the error. Returns {"rescored": n, "<portal>": stats, ...}.
    """
    out: dict = {"rescored": (await rescore())["rescored"]}
    results = await asyncio.gather(*(discover(s, limit, rescore_first=False) for s in sources), return_exceptions=True)
    for source, result in zip(sources, results):
        if isinstance(result, BaseException):
            log.error("%s search failed: %r", source, result)
            result = {"error": f"{type(result).__name__}: {result}"[:300]}
        out[source] = result
    return out


async def rescore() -> dict:
    """Re-score jobs that missed the threshold against the current profile, e.g. after adding a skill."""
    return {"rescored": await _rescore_filtered(Tracker(), _llm(), get_settings())}


def _fresh_enough(p: JobPosting, tracker: Tracker, stats: dict, settings, portal) -> bool:
    fresh = is_fresh(p.posted_at, settings.max_job_age_days)
    if fresh is None and portal.settings.trust_search_age_filter:
        return True  # no date shown, but the portal's search filter only returned recent jobs
    if fresh is False:
        stats["stale"] += 1
        tracker.upsert_job(p, JobStatus.FILTERED_OUT, f"posted '{p.posted_text}' (> {settings.max_job_age_days} days)")
        return False
    if fresh is None and settings.skip_if_date_unknown:
        stats["undated"] += 1
        tracker.upsert_job(p, JobStatus.FILTERED_OUT, "posting date unknown")
        return False
    return True


# ---------------------------------------------------------------- applying

class ApplyState(TypedDict, total=False):
    approved: int
    batches: list[tuple[str, list[dict]]]   # (portal name, jobs to apply to), already trimmed to the caps
    results: list[dict]
    remaining: int


def _plan(state: ApplyState, config: RunnableConfig) -> ApplyState:
    d = _deps(config)
    settings, tracker = d["settings"], d["tracker"]
    approved = tracker.list(status=str(JobStatus.APPROVED))
    remaining = settings.max_applications_per_day - tracker.applied_count_today()
    if d["max_count"] is not None:
        remaining = min(remaining, d["max_count"])
    by_portal: dict[str, list[dict]] = {}
    for j in approved:
        by_portal.setdefault(j["portal"], []).append(j)
    batches, budget = [], remaining
    for portal_name, jobs in by_portal.items():
        # Portals apply in parallel, so the daily cap is shared out up front instead of counted down as we go.
        ps = settings.portals.get(portal_name)
        cap = min(settings.per_portal_cap, ps.daily_cap) if ps and ps.daily_cap is not None else settings.per_portal_cap
        portal_left = cap - tracker.applied_count_today(portal_name)
        if batch := jobs[: max(0, min(budget, portal_left))]:
            batches.append((portal_name, batch))
            budget -= len(batch)
    return {"approved": len(approved), "batches": batches, "results": [], "remaining": remaining}


def _route_plan(state: ApplyState) -> str:
    return "apply_all" if state["batches"] else END


async def _apply_all(state: ApplyState, config: RunnableConfig) -> ApplyState:
    """Every portal applies to its batch at the same time, each in its own browser window."""
    d = _deps(config)
    runs = await asyncio.gather(*(_apply_batch(name, batch, d) for name, batch in state["batches"]),
                                return_exceptions=True)
    results = list(state["results"])
    for (name, _), run in zip(state["batches"], runs):
        if isinstance(run, BaseException):  # the portal could not even open (Chrome, blocked): report, keep the rest
            log.error("Applying on %s failed: %r", name, run)
            results.append({"portal": name, "status": str(JobStatus.FAILED), "reason": str(run)[:200]})
        else:
            results += run
    applied = sum(r["status"] == str(JobStatus.APPLIED) for r in results)
    return {"batches": [], "results": results, "remaining": state["remaining"] - applied}


async def _apply_batch(portal_name: str, batch: list[dict], d: dict) -> list[dict]:
    """Apply to one portal's batch in one browser session."""
    results = []
    async with get_portal(portal_name, d["settings"].portals.get(portal_name)) as portal:
        for j in batch:
            try:
                status, reason = await portal.apply(j["url"])
            except Exception as e:  # keep going with the rest
                log.exception("Apply failed for %s", j["url"])
                status, reason = JobStatus.FAILED, str(e)[:200]
            d["tracker"].set_status(j["id"], status, reason)
            results.append({"id": j["id"], "portal": portal_name, "title": j["title"], "company": j["company"],
                            "status": str(status), "reason": reason})
    return results


def _build_apply_graph():
    g = StateGraph(ApplyState)
    g.add_node("plan", _plan)
    g.add_node("apply_all", _apply_all)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", _route_plan, ["apply_all", END])
    g.add_edge("apply_all", END)
    return g.compile()


APPLY_GRAPH = _build_apply_graph()


async def apply_approved(max_count: int | None = None) -> dict:
    """Apply to approved jobs, respecting daily and per-portal caps."""
    config = {"configurable": {"tracker": Tracker(), "settings": get_settings(), "max_count": max_count}}
    final = await APPLY_GRAPH.ainvoke({}, config=config)
    results, remaining = final["results"], final["remaining"]
    return {"attempted": len(results), "results": results,
            "remaining_today": max(0, remaining), "cap_reached": remaining <= 0 and final["approved"] > len(results)}


# ---------------------------------------------------------------- report and daily run

def daily_report(send: bool = True) -> dict:
    tracker = Tracker()
    today = datetime.now(IST).date().isoformat()
    jobs = tracker.list(since=today, limit=1000)
    path = reporter.build_report(jobs)
    body = reporter.summary_text(jobs)
    sent = False
    if send and get_settings().send_daily_report:
        sent = emailer.send_email(f"Job Copilot daily report - {today}", body, [path])
    return {"report_path": str(path), "emailed": sent, "summary": body}


class DailyState(TypedDict, total=False):
    discovery: dict
    apply: dict | None
    report: dict


def portals_for(source: str) -> list[str]:
    """`all` = every browser portal enabled in settings.yaml; anything else is one portal name."""
    if source != "all":
        return [source]
    portals = get_settings().portals
    return [p for p in BROWSER_PORTALS if p in portals and portals[p].enabled]


async def _daily_discover(state: DailyState, config: RunnableConfig) -> DailyState:
    d = _deps(config)
    return {"discovery": await discover_many(portals_for(d["source"]), d["limit"])}


def _route_daily(state: DailyState) -> str:
    return "apply" if get_settings().mode in AUTO_APPLY_MODES else "report"


async def _daily_apply(state: DailyState) -> DailyState:
    return {"apply": await apply_approved()}


def _daily_report(state: DailyState, config: RunnableConfig) -> DailyState:
    return {"report": daily_report(send=_deps(config)["send_report"])}


def _build_daily_graph():
    g = StateGraph(DailyState)
    g.add_node("discover", _daily_discover)
    g.add_node("apply", _daily_apply)
    g.add_node("report", _daily_report)
    g.add_edge(START, "discover")
    # Auto and hybrid modes apply by themselves; in review mode you approve first (human-in-the-loop).
    g.add_conditional_edges("discover", _route_daily, ["apply", "report"])
    g.add_edge("apply", "report")
    g.add_edge("report", END)
    return g.compile()


DAILY_GRAPH = _build_daily_graph()


async def run_daily(source: str = "all", limit: int | None = None, send_report: bool = True) -> dict:
    config = {"configurable": {"source": source, "limit": limit, "send_report": send_report}}
    final = await DAILY_GRAPH.ainvoke({"apply": None}, config=config)
    return {"discovery": final["discovery"], "apply": final["apply"], "report": final["report"]}
