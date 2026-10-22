"""Rank (and optionally drop) search cards with one LLM call per portal, before any job page is opened.

Discovery can only open `max_jobs_per_run` job pages, and until this step the order came from
`matcher.prescore`, which sees nothing but the title and the portal's tags. A model reading the same
cards recognises "Platform Engineer - Python" as the user's work where exact-token matching does not.

The model is advisory, never authoritative: its scores are blended with `prescore`, a card it failed
to score keeps its deterministic place, and a reply that cannot be reconciled is thrown away.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from job_copilot.agents.matcher import prescore
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.llm.chat import LLM
from job_copilot.models import CardTriage, JobPosting, Profile

log = logging.getLogger(__name__)

TRIAGE_WEIGHT = 0.7     # how much the model's view outweighs the deterministic prescore when re-ranking
FIELD_MAX = 80          # characters kept per card field: titles and tag lists are the token sink
TAGS_PER_CARD = 10
REASON_MAX = 120

SYSTEM = """You are a technical recruiter triaging job-board search cards for one candidate.
You see only the card (title, company, location, experience, salary, tags) - never the job description -
so judge on the title and tags, and stay generous when a card is vague.
- relevance 0-100: how likely this job suits the candidate's roles, skills and years of experience.
  85-100 the title names one of their roles or core skills; 50-80 a different label for the same work
  ("Platform Engineer - Python" for a Python backend role) or an adjacent stack; 0-25 a different
  discipline (data analyst, QA, support, sales), a different stack, or far outside their experience.
- A seniority or location mismatch lowers the score; it never makes it 0 on its own.
- reason: one short line naming the deciding signal, using only words from the card.
- Score every card exactly once, reusing its `i` exactly as given. Never invent cards, ids or fields."""


class TriageReport(BaseModel):
    """What triage did, for the run stats and `-v` logging."""

    used_llm: bool = False
    scored: int = 0
    dropped: int = 0
    lines: list[str] = []  # one debug line per card, in the final ranked order
    notes: list[str] = []  # reconciliation problems worth warning about


def _card_json(card: JobPosting, index: int) -> dict:
    """The card as the model sees it: no url, no id, nothing it could echo back as a fabricated job."""
    fields = {
        "i": index,
        "title": card.title[:FIELD_MAX],
        "company": card.company[:FIELD_MAX],
        "location": card.location[:FIELD_MAX],
        "exp": card.experience_text[:FIELD_MAX],
        "pay": card.salary_text[:FIELD_MAX],
        "tags": [t[:FIELD_MAX] for t in card.tags[:TAGS_PER_CARD] if t.strip()],
    }
    return {k: v for k, v in fields.items() if v or k == "i"}


def _prompt(cards: list[JobPosting], profile: Profile, bank: SkillBank) -> str:
    # Deliberately not `profile_for_llm`: that dumps every project description, experience bullet and
    # education row - hundreds of tokens the model does not need to judge a job title, on every run.
    # This block carries no `personal` data at all, which is the same guarantee at a tenth the size.
    candidate = {
        "roles": profile.preferences.roles,
        "locations": profile.preferences.locations,
        "total_experience_years": profile.total_experience_years,
        "skills": bank.names,
        "open_to_learning": profile.learning_goals,
    }
    body = {"CANDIDATE": candidate, "CARDS": [_card_json(c, i) for i, c in enumerate(cards)]}
    return json.dumps(body, indent=2, ensure_ascii=False)


def _scores(reply: CardTriage, sent: int, notes: list[str]) -> dict[int, tuple[int, str]]:
    """Reconcile the reply against the cards actually sent; anything that doesn't line up is dropped."""
    out: dict[int, tuple[int, str]] = {}
    for item in reply.cards:
        if not 0 <= item.index < sent:
            notes.append(f"ignored card index {item.index}: no such card")
            continue
        if item.index in out:
            notes.append(f"ignored a second score for card {item.index}")
            continue
        out[item.index] = (max(0, min(100, item.relevance)), item.reason.strip()[:REASON_MAX])
    return out


def triage_cards(cards: list[JobPosting], profile: Profile, bank: SkillBank, llm: LLM | None,
                 mode: str = "rank", threshold: int = 40, max_cards: int = 60,
                 ) -> tuple[list[JobPosting], TriageReport]:
    """Re-rank search cards with one batched LLM call; `filter` mode also drops cards below `threshold`.

    Returns the cards in the order their pages should be opened. With no LLM, a failed call or an
    unusable reply, the input order (`matcher.prescore`) comes back unchanged: discovery never
    depends on this step.
    """
    if mode == "off" or llm is None or not cards:
        return cards, TriageReport()

    # Cards past the cap are never reached within `max_jobs_per_run` anyway, so they are not worth tokens.
    head, tail = cards[:max_cards], cards[max_cards:]
    try:
        reply = llm.generate_json(_prompt(head, profile, bank), CardTriage, system=SYSTEM, temperature=0.0)
    except Exception:
        # Rate limits, timeouts, two failed validations, or RedactingLLM refusing an invented placeholder.
        log.warning("Card triage failed; keeping the prescore order", exc_info=True)
        return cards, TriageReport(notes=["triage call failed"])

    notes: list[str] = []
    scored = _scores(reply, len(head), notes)
    report = TriageReport(used_llm=True, scored=len(scored), notes=notes)

    ranked, kept = [], []
    for i, card in enumerate(head):
        base = prescore(card, profile, bank)
        if i in scored:
            relevance, reason = scored[i]
        else:
            # Not scored (a truncated reply is the likeliest failure): fall back to the deterministic
            # view, which makes the key below collapse to exactly `base`. Never a reason to drop a job.
            relevance, reason = round(base * 100), "not scored"
        key = TRIAGE_WEIGHT * (relevance / 100) + (1 - TRIAGE_WEIGHT) * base
        drop = mode == "filter" and i in scored and relevance < threshold
        ranked.append((key, relevance, reason, card, drop))

    ranked.sort(key=lambda r: r[0], reverse=True)  # stable, so prescore order breaks ties
    for _, relevance, reason, card, drop in ranked:
        report.lines.append(f"{'DROP' if drop else '    '} {relevance:3d} {card.title} @ {card.company} - {reason}")
        if drop:
            report.dropped += 1
        else:
            kept.append(card)
    return kept + tail, report
