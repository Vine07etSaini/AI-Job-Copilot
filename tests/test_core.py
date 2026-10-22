from datetime import datetime, timedelta
from pathlib import Path

import pytest

from job_copilot.agents.freshness import is_fresh, parse_posted
from job_copilot.agents.honesty_guard import guard
from job_copilot.agents.matcher import CARD_NEUTRAL, ROLE_FIT_FAMILY, score_job
from job_copilot.agents.screening import answer_question
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import IST, get_profile
from job_copilot.models import (JDAnalysis, ProjectRewrite, ScoreWeights, Settings, SkillRequirement,
                                TailoredContent)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=IST)


@pytest.mark.parametrize("text,days", [
    ("Just now", 0), ("Today", 0), ("Few hours ago", 0), ("1 day ago", 1), ("3 Days Ago", 3),
    ("Posted: 5 days ago", 5), ("30+ days ago", 31), ("a week ago", 7), ("2 weeks ago", 14),
])
def test_parse_relative(text, days):
    assert (NOW - parse_posted(text, NOW)).days == days


def test_parse_absolute_and_unknown():
    assert parse_posted("03 Oct 2026", NOW).date().isoformat() == "2026-10-03"
    assert parse_posted("", NOW) is None
    assert parse_posted("whenever", NOW) is None


def test_is_fresh():
    assert is_fresh(NOW - timedelta(days=6), 7, NOW) is True
    assert is_fresh(NOW - timedelta(days=8), 7, NOW) is False
    assert is_fresh(None, 7, NOW) is None


FIXTURE_PROFILE = Path(__file__).parent / "fixtures" / "profile.yaml"


@pytest.fixture
def profile():
    # A fixed example profile, so tests don't depend on (or reveal) the real config/profile.yaml.
    return get_profile(FIXTURE_PROFILE)


@pytest.fixture
def bank(profile):
    return SkillBank(profile)


def test_skill_bank_aliases_and_equivalents(bank):
    assert bank.canonical("python3") == "Python"
    assert bank.canonical("RESTful API") == "REST APIs"
    assert bank.match("GCP") == ("AWS", True)
    assert bank.match("Kubernetes") is None


def jd(mandatory, nice=(), lo=None, hi=None):
    return JDAnalysis(
        role_title="Software Engineer",  # generic: no title bonus, no role penalty
        mandatory_skills=[SkillRequirement(skill=s, alternatives=alts) for s, alts in mandatory],
        nice_to_have_skills=list(nice), min_experience_years=lo, max_experience_years=hi,
        keywords=[], summary="",
    )


def test_score_good_and_bad(profile, bank):
    good = score_job(profile, jd([("Python", []), ("GCP", ["AWS"]), ("PostgreSQL", [])], ["Docker"], 3, 6),
                     bank, ScoreWeights())
    assert good.score == 100 and not good.missing_mandatory

    bad = score_job(profile, jd([("Java", []), ("Spring Boot", []), ("Python", [])], [], 6, 10), bank, ScoreWeights())
    assert bad.score < 70
    assert bad.missing_mandatory == ["Java", "Spring Boot"]
    assert "missing Java" in bad.explanation


def test_score_does_not_reward_a_sparse_jd(profile, bank):
    empty = score_job(profile, jd([]), bank, ScoreWeights())
    assert empty.score == 0 and "no skills" in empty.explanation
    one = score_job(profile, jd([("Python", [])]), bank, ScoreWeights())
    assert one.score < 70 and "only 1 skill" in one.explanation  # 1/1 matched is not proof of a good fit
    # no nice-to-haves and no experience stated: those parts are left out, not counted as 100% (was 70 before)
    half = score_job(profile, jd([("Python", []), ("Java", []), ("Go", []), ("SQL", [])]), bank, ScoreWeights())
    assert half.score == 50


@pytest.mark.parametrize("title,company,fit", [
    ("Software Engineer II", "", 1.0),
    ("Python Developer @ Infosys", "", 1.0),
    ("Back-End Engineer - Payments", "Acme", 1.0),
    ("GenAI Developer", "", 1.0),  # "Generative AI" is an alias of a skill you have
    ("Software Development Engineer II - Unosecur", "Unosecur Technologies", 1.0),
    ("Java Developer", "", ROLE_FIT_FAMILY),      # engineering, in a stack you don't list
    ("Golang Developer", "", ROLE_FIT_FAMILY),
    ("Data Analyst", "", 0.0),                    # another line of work entirely
    ("Tax Documentation - Analyst", "", 0.0),
])
def test_role_fit_from_title(profile, title, company, fit):
    from job_copilot.agents.matcher import role_fit

    assert role_fit(title, company, profile) == fit


def test_skill_name_forms_match_abbreviations_and_plurals(profile):
    from job_copilot.agents.matcher import role_fit
    from job_copilot.models import Skill

    p = profile.model_copy(update={"skills": [Skill(name="Retrieval-Augmented Generation (RAG)"),
                                              Skill(name="React.js"), Skill(name="AI Agents")]})
    bank = SkillBank(p)
    assert bank.canonical("RAG") == bank.canonical("retrieval augmented generation") == p.skills[0].name
    assert bank.canonical("React") == bank.canonical("ReactJS") == "React.js"
    assert role_fit("Agent Engineer", "", p) == role_fit("ReactJS Developer", "", p) == 1.0


def test_title_outside_roles_halves_the_score(profile, bank):
    analysis = jd([("Python", []), ("SQL", []), ("Pandas", []), ("Git", [])]).model_copy(
        update={"role_title": "Data Analyst"})
    m = score_job(profile, analysis, bank, ScoreWeights())
    assert m.score == 50 and m.role_fit == 0 and "outside your roles" in m.explanation


def test_core_skills_weigh_more_and_skill_years_count(profile, bank):
    from job_copilot.agents.matcher import skill_weight
    from job_copilot.models import JobPosting

    java = SkillRequirement(skill="Java", alternatives=[])
    assert skill_weight(java, "Java Developer", "Java, Java and more Java.") == 4  # title + two extra mentions
    assert skill_weight(java, "Developer", "Some Java.") == 1
    analysis = jd([("Java", []), ("Python", []), ("SQL", []), ("Git", [])])
    posting = JobPosting(portal="t", external_id="1", title="Software Engineer", company="X", url="")
    light = score_job(profile, analysis, bank, ScoreWeights(), posting.model_copy(update={"description": "Java"}))
    heavy = score_job(profile, analysis, bank, ScoreWeights(),
                      posting.model_copy(update={"description": "Java. Java. Java. Python, SQL, Git."}))
    assert heavy.score < light.score  # the missing skill is the one the JD keeps repeating
    needs = jd([("Python", []), ("SQL", []), ("Git", []), ("Docker", [])])
    needs.mandatory_skills[0].min_years = 6  # profile has Python 4y: lose 50% per missing year
    m = score_job(profile, needs, bank, ScoreWeights())
    assert m.score == 75 and "Python 4y vs 6y" in m.explanation


def test_skill_years_and_title_exclusion():
    from job_copilot.agents.jd_analyzer import skill_years
    from job_copilot.agents.matcher import excluded_title_word

    assert skill_years("Need 3+ years of hands-on Python.", "Python") == 3
    assert skill_years("Python (2 yrs) is a must", "Python") == 2
    assert skill_years("2-5 years of experience with Python", "Python") is None  # total experience, not Python
    assert excluded_title_word("Sr. Python Developer", ["senior", "sr"]) == "sr"
    assert excluded_title_word("Python Developer", ["senior", "sr"]) is None


def test_heuristic_analysis_uses_jd_sections_not_raw_tags(bank):
    from job_copilot.agents.jd_analyzer import heuristic_analysis
    from job_copilot.models import JobPosting

    posting = JobPosting(portal="t", external_id="1", title="Python Developer", company="X", url="",
                         tags=["Python", "Communication Skills", "Kafka", "Docker", "Kubernetes"],
                         description="Requirements:\n- Strong Python and SQL\n- Kafka streaming\n"
                                     "Nice to have:\n- Docker and AWS\n")
    a = heuristic_analysis(posting, bank)
    assert [r.skill for r in a.mandatory_skills] == ["Python", "Kafka", "SQL"]
    assert a.nice_to_have_skills == ["Docker", "Kubernetes", "AWS"]  # soft skill dropped, unmentioned tag demoted


def test_prescore_ranks_relevant_cards_first(profile, bank):
    from job_copilot.agents.matcher import prescore
    from job_copilot.models import JobPosting

    card = JobPosting(portal="t", external_id="1", title="Java Developer", company="X", url="", tags=["Java"])
    good = card.model_copy(update={"title": "Python Developer", "tags": ["Python", "Django"]})
    off_role = card.model_copy(update={"title": "Tax Documentation - Analyst"})
    ranked = sorted([card, good, off_role], key=lambda c: prescore(c, profile, bank), reverse=True)
    assert [c.title for c in ranked] == ["Python Developer", "Java Developer", "Tax Documentation - Analyst"]
    assert prescore(good, profile, bank) == 1.0          # your role, every tag a skill you have
    assert prescore(card, profile, bank) == 0.25         # engineering, but neither the title nor the tag is yours
    assert prescore(off_role, profile, bank) == 0.0


def test_prescore_uses_the_card_snippet_when_there_are_no_tags(profile, bank):
    """Indeed and LinkedIn cards carry no tags, so without this they would all prescore the same."""
    from job_copilot.agents.matcher import card_evidence, prescore
    from job_copilot.models import JobPosting

    card = JobPosting(portal="t", external_id="1", title="Python Developer", company="X", url="")
    rich = card.model_copy(update={"snippet": "Build APIs with Python, Django and PostgreSQL."})
    thin = card.model_copy(update={"snippet": "Build APIs with Python."})
    silent = card.model_copy(update={"snippet": "A fast-growing team in a modern office."})
    tagged = card.model_copy(update={"tags": ["Python", "Django"]})

    ranked = sorted([silent, thin, rich], key=lambda c: prescore(c, profile, bank), reverse=True)
    assert [c.snippet[:10] for c in ranked] == [rich.snippet[:10], thin.snippet[:10], silent.snippet[:10]]
    # Promote-only is the invariant: a snippet is truncated, so naming none of your skills proves nothing.
    assert card_evidence(silent, bank) == card_evidence(card, bank) == CARD_NEUTRAL
    assert card_evidence(rich, bank) == 1.0
    # A tag list, by contrast, is the recruiter's complete list, so a tag you lack may sink the card.
    assert card_evidence(tagged.model_copy(update={"tags": ["Java"]}), bank) == 0.0


def test_role_fit_grades_an_off_stack_engineering_title_between_the_two_extremes(profile, bank):
    analysis = jd([("Python", []), ("SQL", []), ("Pandas", []), ("Git", [])])
    on_role = score_job(profile, analysis.model_copy(update={"role_title": "Python Developer"}), bank, ScoreWeights())
    off_stack = score_job(profile, analysis.model_copy(update={"role_title": "Golang Developer"}), bank, ScoreWeights())
    off_work = score_job(profile, analysis.model_copy(update={"role_title": "Data Analyst"}), bank, ScoreWeights())
    assert off_work.score < off_stack.score < on_role.score
    assert "outside your stack" in off_stack.explanation and "outside your roles" in off_work.explanation


def test_skill_bank_finds_skills_in_text(bank):
    found = bank.skills_in("We use Postgres and ReactJS daily; REST APIs everywhere. No Rust here.")
    assert "PostgreSQL" in found and "Rust" not in found


def test_tracker_migrates_an_old_database_and_round_trips_tags(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` never adds a column, so an existing tracker.db needs an ALTER."""
    import sqlite3

    from job_copilot.db import Tracker
    from job_copilot.models import JobPosting, JobStatus

    db = tmp_path / "old.db"
    before = sqlite3.connect(db)
    before.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, portal TEXT NOT NULL, external_id TEXT NOT NULL,
            title TEXT NOT NULL, company TEXT NOT NULL, location TEXT, url TEXT NOT NULL, experience_text TEXT,
            salary_text TEXT, posted_at TEXT, description TEXT, recruiter_email TEXT, recruiter_phone TEXT,
            analysis_json TEXT, match_score INTEGER, match_json TEXT, resume_path TEXT, resume_url TEXT,
            guard_json TEXT, status TEXT NOT NULL, status_reason TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, applied_at TEXT);""")
    before.execute("INSERT INTO jobs (id, fingerprint, portal, external_id, title, company, url, status,"
                   " created_at, updated_at) VALUES ('old1', 'fp', 'naukri', '1', 'Python Developer', 'Acme',"
                   " 'u', 'filtered_out', 't', 't')")
    before.commit()
    before.close()

    tracker = Tracker(db)
    assert tracker.get("old1")["title"] == "Python Developer"  # the user's data survived the migration

    posting = JobPosting(portal="naukri", external_id="2", title="Backend Developer", company="X", url="u",
                         tags=["Python", "Django"], posted_text="2 days ago")
    jid = tracker.upsert_job(posting, JobStatus.PENDING_REVIEW)
    assert tracker.get(jid)["tags"] == ["Python", "Django"] and tracker.get(jid)["posted_text"] == "2 days ago"
    # A re-score rebuilds the posting from the row and upserts it again; that must not erase what search found.
    tracker.upsert_job(posting.model_copy(update={"tags": [], "posted_text": ""}), JobStatus.APPROVED)
    assert tracker.get(jid)["tags"] == ["Python", "Django"] and tracker.get(jid)["status"] == "approved"


def test_guard_blocks_invented_skills(profile, bank):
    analysis = jd([("Python", []), ("Kafka", []), ("Kubernetes", [])])
    draft = TailoredContent(
        summary="Python engineer experienced with Kafka.",
        skills=["Python", "Kafka", "AWS"],
        projects=[
            ProjectRewrite(project_id="order-service",
                           description="Order API in Python and FastAPI deployed with Kubernetes."),
            ProjectRewrite(project_id="job-copilot", description="Python agent using Playwright and an LLM."),
            ProjectRewrite(project_id="made-up", description="Nope"),
        ],
        keywords_used=["Python"],
    )
    cleaned, report = guard(draft, profile, analysis, bank)
    assert not report.ok
    assert "Kafka" not in cleaned.skills and cleaned.skills[:2] == ["Python", "AWS"]
    assert cleaned.summary == profile.summary  # reverted
    by_id = {p.project_id: p.description for p in cleaned.projects}
    assert "Kubernetes" not in by_id["order-service"]  # reverted to original
    assert by_id["job-copilot"] == "Python agent using Playwright and an LLM."  # honest rewrite kept
    assert "made-up" not in by_id


def test_guard_blocks_keyword_stuffing(profile, bank):
    analysis = jd([("Python", [])])
    draft = TailoredContent(summary=profile.summary, skills=["Python"], keywords_used=[], projects=[
        ProjectRewrite(project_id="job-copilot", description="Python Python Python agent in Python."),
    ])
    cleaned, report = guard(draft, profile, analysis, bank, max_repeats=2)
    assert any("stuffing" in v for v in report.violations)


def test_portal_credentials_from_env(monkeypatch):
    from job_copilot.config import get_portal_credentials

    monkeypatch.setenv("NAUKRI_EMAIL", "me@example.com")
    monkeypatch.setenv("NAUKRI_PASSWORD", "secret")
    assert get_portal_credentials("naukri").configured
    monkeypatch.delenv("NAUKRI_PASSWORD")
    assert not get_portal_credentials("naukri").configured


# ---------------------------------------------------------------- resume import

RESUME_TEXT = "Asha Rao asha@example.com\nPython and PostgreSQL developer. Built Order Service with FastAPI."


def _extract(skills):
    from job_copilot.models import Education, Experience, ExtractedProject, Personal, ResumeExtract

    return ResumeExtract(
        personal=Personal(name="Asha Rao", email="asha@example.com"),
        skills=skills,
        projects=[ExtractedProject(name="Order Processing Service", tech=["FastAPI"], description="REST API.")],
        experience=[Experience(title="Engineer", company="Acme", start="Jan 2020", end="Present")],
        education=[Education(degree="B.Tech", institution="IIT", year="2019")],
    )


def test_import_drops_skills_not_in_pdf_text():
    from job_copilot.agents.profile_importer import verify_skills

    extract, dropped = verify_skills(_extract(["Python", "FastAPI", "Kubernetes", "python"]), RESUME_TEXT)
    assert extract.skills == ["Python", "FastAPI"]  # de-duplicated, hallucinated Kubernetes removed
    assert dropped == ["Kubernetes"]


def test_import_merge_keeps_preferences_and_replaces_locked_sections(profile):
    from job_copilot.agents.profile_importer import merge_into_profile

    merged = merge_into_profile(_extract(["Python", "REST APIs"]), profile)
    assert merged.preferences == profile.preferences
    assert merged.equivalents == profile.equivalents and merged.learning_goals == profile.learning_goals
    assert [e.company for e in merged.experience] == ["Acme"]
    assert merged.personal.name == "Asha Rao"
    py = next(s for s in merged.skills if s.name == "Python")
    assert py.aliases == ["python3"] and py.years is None  # aliases carried over, years never inferred
    assert "REST" in next(s for s in merged.skills if s.name == "REST APIs").aliases


def test_import_project_ids_and_missing_file(profile, tmp_path):
    from job_copilot.agents.profile_importer import extract_pdf_text, merge_into_profile

    merged = merge_into_profile(_extract(["Python"]), profile)
    assert merged.projects[0].id == "order-service"  # matched to the existing project by name
    assert merged.projects[0].github == "https://github.com/your-handle/order-service"
    with pytest.raises(ValueError, match="not found"):
        extract_pdf_text(tmp_path / "nope.pdf")


def test_is_known_forgets_old_filtered_rows(tmp_path, monkeypatch):
    """A job skipped for its age must be able to come back when it is re-posted; a decision must not."""
    from datetime import timedelta

    from job_copilot import db
    from job_copilot.db import Tracker
    from job_copilot.models import JobPosting, JobStatus

    tracker = Tracker(tmp_path / "t.db")
    stale = JobPosting(portal="naukri", external_id="1", title="Python Developer", company="Acme", url="u")
    decided = stale.model_copy(update={"external_id": "2", "title": "Backend Developer"})
    # Tracker.update always stamps the current time, so the row has to be backdated as it is written.
    monkeypatch.setattr(db, "now_iso", lambda: (datetime.now(IST) - timedelta(days=20)).isoformat(timespec="seconds"))
    tracker.upsert_job(stale, JobStatus.FILTERED_OUT, "posted '21 days ago' (> 7 days)")
    tracker.upsert_job(decided, JobStatus.APPLIED)
    monkeypatch.undo()

    assert not tracker.is_known(stale, 14)       # 20 days old and only ever filtered: look again
    assert tracker.is_known(stale, 0)            # 0 = never forget, the behaviour before this setting existed
    assert tracker.is_known(stale)
    assert tracker.is_known(decided, 14)         # already applied to: never offer it again
    assert not tracker.is_known(stale.model_copy(update={"external_id": "9", "title": "Rust Developer"}), 14)


@pytest.mark.parametrize("text,expected", [
    ("3-6 Lacs PA", (3.0, 6.0)),
    ("₹ 5,00,000 - 8,00,000 P.A.", (5.0, 8.0)),      # Indian digit grouping, rupees per year
    ("12 LPA", (12.0, 12.0)),
    ("Up to 10 Lacs", (None, 10.0)),                   # a ceiling, not a range
    ("₹30,000 - ₹45,000 a month", (3.6, 5.4)),
    ("Not disclosed", (None, None)),
    ("", (None, None)),
    ("2 openings", (None, None)),                      # a bare number with no salary marker is not a salary
])
def test_parse_lpa_reads_the_formats_the_portals_use(text, expected):
    from job_copilot.agents.salary import parse_lpa

    assert parse_lpa(text) == expected


def test_salary_floor_only_filters_a_stated_low_package():
    from job_copilot.agents.salary import meets_salary_floor

    assert not meets_salary_floor("2-4 Lacs PA", 6)           # states a package, top of range below the floor
    assert meets_salary_floor("5-9 Lacs PA", 6)               # top of the range clears it
    assert meets_salary_floor("Not disclosed", 6)             # unknown always passes: the Indian default
    assert meets_salary_floor("2-4 Lacs PA", None)            # no floor set, nothing to enforce


@pytest.mark.parametrize("company,blocked", [
    ("Acme Technologies Pvt. Ltd.", True),
    ("ACME", True),
    ("Acmetron Labs", False),       # whole tokens only, never a substring
    ("Beta Systems", False),
])
def test_blacklist_matches_company_name_variants(company, blocked):
    from job_copilot.agents.matcher import blacklisted_company

    assert (blacklisted_company(company, ["Acme"]) == "Acme") is blocked
    # A blank or suffix-only entry must not blacklist every company there is.
    assert blacklisted_company(company, ["", "Pvt Ltd"]) is None


def test_pick_skips_blacklisted_and_low_salary_cards(temp_pipeline, profile):
    from job_copilot.models import JobPosting

    tracker = temp_pipeline.Tracker()
    cards = [JobPosting(portal="t", external_id="1", title="Python Developer", company="Acme Pvt Ltd", url="u"),
             JobPosting(portal="t", external_id="2", title="Python Developer", company="Beta", url="u",
                        salary_text="2-4 Lacs PA")]
    settings = Settings(blacklist_companies=["Acme"], exclude_title_words=[])
    profile = profile.model_copy(update={"preferences": profile.preferences.model_copy(update={"min_salary_lpa": 6})})
    state = {"cards": cards, "index": 0, "processed": 0,
             "stats": {"blacklisted": 0, "duplicate": 0, "excluded_title": 0, "low_salary": 0}}
    config = {"configurable": {"settings": settings, "tracker": tracker, "profile": profile, "limit": 10}}

    update = temp_pipeline._pick(state, config)
    assert update["current"] is None  # neither card is worth a page load
    assert update["stats"]["blacklisted"] == 1 and update["stats"]["low_salary"] == 1
    row = next(r for r in tracker.list(status="filtered_out") if r["company"] == "Beta")
    assert "below 6 LPA" in row["status_reason"]


def test_keep_paging_stops_on_the_cap_the_target_and_an_empty_page():
    from job_copilot.models import PortalSettings
    from job_copilot.portals.sample import SamplePortal

    portal = SamplePortal(PortalSettings(pages_per_search=3, min_new_cards_per_page=5))
    assert portal.keep_paging(1, 20, 20, 120)      # room left everywhere: keep going
    assert not portal.keep_paging(3, 20, 60, 120)  # page cap reached
    assert not portal.keep_paging(1, 2, 20, 120)   # the site has run out of results for this query
    assert not portal.keep_paging(1, 20, 120, 120)  # the run already has the cards it asked for
    assert portal.keep_paging(1, 20, 999, 0)       # 0 = no target, only the cap and the empty-page rule apply


# ---------- card triage ----------

class _TriageLLM:
    """Scripted triage model: returns a fixed CardTriage (or raises), and records the prompts it was sent."""

    label = "Fake"

    def __init__(self, cards=(), error=None):
        self.cards, self.error, self.sent = list(cards), error, []

    def generate_json(self, prompt, schema, system=None, temperature=0.2):
        self.sent += [prompt, system or ""]
        if self.error:
            raise self.error
        return schema(cards=self.cards)

    def generate_text(self, prompt, system=None, temperature=0.4):
        raise AssertionError("triage must not call generate_text")


def _cards(*titles):
    from job_copilot.models import JobPosting

    return [JobPosting(portal="t", external_id=str(i), title=t, company="X", url="")
            for i, t in enumerate(titles)]


def _triaged(*scores):
    from job_copilot.models import TriagedCard

    return [TriagedCard(index=i, relevance=r, reason="because") for i, r in scores]


def _run(cards, profile, bank, llm, **kw):
    from job_copilot.agents.card_triage import triage_cards

    return triage_cards(cards, profile, bank, llm, **kw)


def test_triage_lifts_an_unusual_title_above_the_prescore_order(profile, bank):
    """The whole point: prescore cannot tell "Platform Engineer - Python" from an unrelated job."""
    cards = _cards("Data Analyst", "Python Developer", "Platform Engineer - Python")
    llm = _TriageLLM(_triaged((0, 10), (1, 80), (2, 95)))
    out, report = _run(cards, profile, bank, llm)
    assert [c.title for c in out] == ["Platform Engineer - Python", "Python Developer", "Data Analyst"]
    assert report.used_llm and report.scored == 3 and report.dropped == 0


def test_triage_rank_mode_never_drops(profile, bank):
    cards = _cards("Python Developer", "Warehouse Supervisor")
    out, report = _run(cards, profile, bank, _TriageLLM(_triaged((0, 90), (1, 5))), mode="rank")
    assert len(out) == len(cards) and report.dropped == 0


def test_triage_filter_drops_only_below_the_threshold(profile, bank):
    cards = _cards("Python Developer", "Warehouse Supervisor")
    out, report = _run(cards, profile, bank, _TriageLLM(_triaged((0, 55), (1, 10))), mode="filter", threshold=40)
    assert [c.title for c in out] == ["Python Developer"] and report.dropped == 1


def test_triage_ignores_hallucinated_and_duplicate_indices(profile, bank):
    from job_copilot.models import TriagedCard

    cards = _cards("Python Developer", "Backend Developer")
    reply = [TriagedCard(index=99, relevance=90, reason="invented"),
             TriagedCard(index=-1, relevance=90, reason="invented"),
             TriagedCard(index=0, relevance=150, reason="over the top"),
             TriagedCard(index=0, relevance=5, reason="second opinion")]
    out, report = _run(cards, profile, bank, _TriageLLM(reply), mode="filter", threshold=40)
    assert len(out) == 2 and report.scored == 1 and len(report.notes) == 3  # two bad indices, one duplicate
    assert "100" in report.lines[0]  # clamped, and the first score for a card wins


def test_triage_keeps_cards_the_model_omitted(profile, bank):
    """A truncated reply is the likeliest failure; an unscored card must never be treated as a rejected one."""
    cards = _cards("Python Developer", "Backend Developer", "Django Developer")
    out, report = _run(cards, profile, bank, _TriageLLM(_triaged((0, 90),)), mode="filter", threshold=40)
    assert len(out) == 3 and report.dropped == 0 and report.scored == 1


def test_triage_without_an_llm_keeps_the_prescore_order(profile, bank):
    cards = _cards("Python Developer", "Data Analyst")
    out, report = _run(cards, profile, bank, None, mode="filter")
    assert out == cards and not report.used_llm and report.scored == 0


def test_triage_survives_an_llm_error(profile, bank):
    cards = _cards("Python Developer", "Data Analyst")
    out, report = _run(cards, profile, bank, _TriageLLM(error=RuntimeError("429 rate limited")))
    assert out == cards and report.scored == 0 and "failed" in report.notes[0]


def test_triage_sends_cards_only_and_no_personal_details(profile, bank):
    from job_copilot.llm.redact import RedactingLLM, profile_pii

    cards = _cards("Python Developer")
    cards[0].description = "secret JD text that has not been fetched yet"
    fake = _TriageLLM(_triaged((0, 90)))
    _run(cards, _pii_profile(), bank, RedactingLLM(fake, lambda: profile_pii(_pii_profile())))
    prompt = fake.sent[0]
    assert "Python Developer" in prompt and "secret JD text" not in prompt
    assert not any(v.lower() in prompt.lower() for v in PII_VALUES)


def test_triage_node_is_a_no_op_without_an_llm(temp_pipeline, profile):
    settings = Settings(triage="filter")
    state = {"cards": _cards("Python Developer"), "index": 0, "stats": {"triage_scored": 0, "triage_dropped": 0}}
    config = {"configurable": {"settings": settings, "profile": profile, "llm": None}}
    assert temp_pipeline._triage(state, config) == {}


def test_triage_node_reports_what_it_dropped(temp_pipeline, profile):
    from types import SimpleNamespace

    settings = Settings(triage="filter", triage_threshold=40)
    state = {"cards": _cards("Python Developer", "Warehouse Supervisor"), "index": 3,
             "stats": {"triage_scored": 0, "triage_dropped": 0}}
    config = {"configurable": {"settings": settings, "profile": profile,
                               "llm": _TriageLLM(_triaged((0, 90), (1, 5))),
                               "portal": SimpleNamespace(name="sample")}}
    update = temp_pipeline._triage(state, config)
    assert [c.title for c in update["cards"]] == ["Python Developer"]
    assert update["stats"]["triage_dropped"] == 1 and update["stats"]["triage_scored"] == 2
    assert update["index"] == 0  # the list changed under _pick, so the cursor has to restart


# ---------- screening questions ----------

def _answer(profile, bank, question, options=(), **prefs):
    profile = profile.model_copy(update={"preferences": profile.preferences.model_copy(update=prefs)})
    return answer_question(question, list(options), profile, bank)


@pytest.mark.parametrize("question,expected", [
    ("Do you have hands-on experience developing production-grade applications using Python?", "Yes"),
    ("Do you have experience in Python and FastAPI?", "Yes"),
    ("Do you have experience in Python and Cobol?", None),    # a second requirement we can't verify
    ("Do you have hands-on experience with Cobol?", None),    # skill not in the bank
    ("Do you have experience with Python, Cobol or Fortran?", "Yes"),   # alternatives: any one you have is true
    ("Do you have experience with Python, Cobol and Fortran?", None),
    ("Are you comfortable with night shifts?", None),
])
def test_screening_skill_questions(profile, bank, question, expected):
    assert _answer(profile, bank, question, ["Yes", "No"]) == expected


def test_screening_profile_facts(profile, bank):
    assert _answer(profile, bank, "What is your notice period (days)?", notice_period_days=30) == "30"
    assert _answer(profile, bank, "What is your notice period?", notice_period_days=None) is None
    assert _answer(profile, bank, "What is your expected CTC?", expected_ctc_lpa=None) is None
    assert _answer(profile, bank, "What is your expected CTC in LPA?", expected_ctc_lpa=7.5) == "7.5"
    assert _answer(profile, bank, "Are you willing to relocate?", ["Yes", "No"], relocate=None) is None
    assert _answer(profile, bank, "Are you willing to relocate?", ["Yes", "No"], relocate=False) == "No"
    assert _answer(profile, bank, "Are you willing to relocate?", ["Maybe"], relocate=True) is None


def test_screening_education_picks_highest_degree(profile, bank):
    options = ["Diploma", "Bachelors Degree", "Masters Degree", "MBA"]
    degree = profile.education[0].model_copy(update={"degree": "Bachelor of Computer Applications (BCA)"})
    p = profile.model_copy(update={"education": [degree]})
    assert answer_question("Highest Level of Education Obtained", options, p, bank) == "Bachelors Degree"
    assert answer_question("Highest Level of Education Obtained", ["Doctorate"], p, bank) is None


# ---------- LangGraph pipeline ----------

@pytest.fixture
def temp_pipeline(tmp_path, monkeypatch):
    """pipeline with a throwaway tracker, no Gemini and a fake PDF step (offline)."""
    from job_copilot import pipeline
    from job_copilot.db import Tracker

    async def fake_build_resume(profile, posting, content, jid, analysis=None, report=None):
        pdf = tmp_path / f"{jid}.pdf"
        pdf.write_bytes(b"%PDF")
        return pdf, pdf.as_uri()

    monkeypatch.setattr(pipeline, "Tracker", lambda: Tracker(tmp_path / "t.db"))
    monkeypatch.setattr(pipeline, "_llm", lambda: None)
    monkeypatch.setattr(pipeline, "build_resume", fake_build_resume)
    return pipeline


def test_discover_graph_processes_sample_jobs(temp_pipeline):
    import asyncio

    stats = asyncio.run(temp_pipeline.discover("sample"))
    assert stats["found"] > 0 and stats["errors"] == 0
    handled = sum(stats[k] for k in ("filtered_out", "pending_review", "approved", "stale", "undated", "duplicate",
                                     "excluded_title", "triage_dropped", "low_salary"))
    assert handled == stats["found"]
    # a second run sees everything as already known: the loop terminates and de-dupes
    again = asyncio.run(temp_pipeline.discover("sample"))
    assert again["duplicate"] == again["found"] and again["pending_review"] == 0


def test_rescore_tailors_jobs_that_pass_after_a_profile_change(temp_pipeline, profile, monkeypatch):
    import asyncio

    from job_copilot.models import JobPosting, JobStatus, Skill

    tracker = temp_pipeline.Tracker()
    posting = JobPosting(portal="t", external_id="1", title="Python Developer", company="X", url="u",
                         posted_at=datetime.now(IST), description="Python, Kubernetes, Terraform, Docker")
    analysis = jd([("Python", []), ("Kubernetes", []), ("Terraform", []), ("Docker", [])])
    jid = tracker.upsert_job(posting, JobStatus.FILTERED_OUT, "score 67 < 70", analysis_json=analysis, match_score=67)
    stale = posting.model_copy(update={"external_id": "2"})
    old = tracker.upsert_job(stale, JobStatus.FILTERED_OUT, "posted '30+ days ago'", analysis_json=analysis)

    monkeypatch.setattr(temp_pipeline, "get_profile", lambda: profile)
    assert asyncio.run(temp_pipeline.rescore()) == {"rescored": 0}  # same profile: still below the threshold

    learned = [*profile.skills, Skill(name="Kubernetes"), Skill(name="Terraform")]
    monkeypatch.setattr(temp_pipeline, "get_profile", lambda: profile.model_copy(update={"skills": learned}))
    assert asyncio.run(temp_pipeline.rescore()) == {"rescored": 1}
    assert tracker.get(jid)["status"] == "pending_review" and tracker.get(jid)["match_score"] == 100
    assert tracker.get(old)["status"] == "filtered_out"  # stale jobs are not revived


def test_resume_gaps_ignore_the_skills_list(profile, bank):
    from job_copilot.agents.resume_builder import resume_gaps

    match = score_job(profile, jd([("Python", []), ("Django", []), ("GCP", ["AWS"]), ("Pandas", [])]), bank,
                      ScoreWeights())
    # Python and AWS appear in the summary/projects; Django and Pandas are only in the skills list
    assert resume_gaps(profile, match, bank) == ["Django", "Pandas"]


def test_hybrid_mode_auto_approves_covered_jobs_and_reviews_the_rest(temp_pipeline, profile, monkeypatch):
    import asyncio

    from job_copilot.models import JobPosting

    monkeypatch.setattr(temp_pipeline, "get_profile", lambda: profile)
    hybrid = temp_pipeline.get_settings().model_copy(update={"mode": "hybrid"})
    monkeypatch.setattr(temp_pipeline, "get_settings", lambda: hybrid)
    tracker = temp_pipeline.Tracker()
    posting = JobPosting(portal="t", external_id="1", title="Software Engineer", company="X", url="u")

    covered = jd([("Python", []), ("AWS", []), ("Docker", []), ("PostgreSQL", [])])
    jid, status = asyncio.run(temp_pipeline.process_posting(posting, tracker, None, covered))
    assert status == "approved" and tracker.get(jid)["resume_path"] is None  # no new resume: Naukri's is used

    gap = jd([("Python", []), ("Django", []), ("Pandas", []), ("SQL", [])])
    other = posting.model_copy(update={"external_id": "2"})
    jid, status = asyncio.run(temp_pipeline.process_posting(other, tracker, None, gap))
    row = tracker.get(jid)
    assert status == "pending_review" and row["resume_path"]
    assert row["status_reason"] == "new resume needed: your resume doesn't show Django, Pandas"
    assert temp_pipeline._route_daily({}) == "apply"


def test_report_lists_auto_applied_and_review_jobs():
    from job_copilot.agents.reporter import summary_text

    jobs = [{"status": "applied", "match_score": 90, "title": "Dev", "company": "A", "status_reason": "applied"},
            {"status": "pending_review", "match_score": 75, "title": "Eng", "company": "B",
             "status_reason": "new resume needed: your resume doesn't show Django"}]
    text = summary_text(jobs)
    assert "Applied automatically (1):" in text and "[90] Dev @ A" in text
    assert "Waiting for your review" in text and "doesn't show Django" in text


def test_indeed_search_url_and_posted_text():
    from job_copilot.portals.indeed import clean_posted, search_url

    assert search_url("https://in.indeed.com", "Python Developer", "Bengaluru", 0, 7) == \
        "https://in.indeed.com/jobs?q=Python+Developer&l=Bengaluru&fromage=7"
    assert search_url("https://in.indeed.com", "Backend Developer", "Remote", 10, 3).endswith("&fromage=3&start=10")
    assert clean_posted("PostedPosted 3 days ago") == "3 days ago"
    assert clean_posted("Posted 30+ days ago") == "30+ days ago"
    assert clean_posted("Just posted") == "Just posted"
    assert clean_posted("Employer active 2 days ago") == ""  # employer activity, not the posting date
    assert (NOW - parse_posted(clean_posted("Posted 3 days ago"), NOW)).days == 3


def test_indeed_is_registered_and_shares_the_browser_settings():
    from job_copilot.config import get_settings
    from job_copilot.pipeline import portals_for
    from job_copilot.portals.base import get_portal
    from job_copilot.portals.indeed import IndeedPortal

    portal = get_portal("indeed", get_settings().portals.get("indeed"))
    assert isinstance(portal, IndeedPortal) and portal.base == "https://in.indeed.com"
    assert portal.on_login_page("https://secure.indeed.com/auth?continue=x")
    assert not portal.on_login_page("https://in.indeed.com/viewjob?jk=1")
    assert portals_for("indeed") == ["indeed"] and "indeed" in portals_for("all") and "sample" not in portals_for("all")


def test_linkedin_search_url_posted_text_and_login_pages():
    from job_copilot.config import get_settings
    from job_copilot.portals.base import BROWSER_PORTALS, get_portal
    from job_copilot.portals.linkedin import LinkedInPortal, clean_posted, search_url

    url = search_url("Python Developer", "Bengaluru", 0, 7, True)
    assert "keywords=Python+Developer" in url and "f_TPR=r604800" in url and "f_AL=true" in url
    assert "f_WT=2" in search_url("Backend Developer", "Remote", 25, 3, False)
    assert "start=25" in search_url("Dev", "Pune", 25, 3, False) and "f_AL" not in search_url("Dev", "", 0, 3, False)
    assert clean_posted("Bengaluru · Reposted 3 days ago · 50 applicants") == "3 days ago"
    assert clean_posted("2026-10-08") == "2026-10-08" and clean_posted("Promoted") == ""
    portal = get_portal("linkedin", get_settings().portals.get("linkedin"))
    assert isinstance(portal, LinkedInPortal) and "linkedin" in BROWSER_PORTALS
    assert portal.on_login_page("https://www.linkedin.com/login/?session_redirect=x")
    assert portal.on_login_page("https://www.linkedin.com/checkpoint/challenge/abc")
    assert not portal.on_login_page("https://www.linkedin.com/jobs/view/123/")


def test_portal_daily_cap_limits_linkedin_applications(temp_pipeline):
    from job_copilot.models import JobPosting, JobStatus, PortalSettings

    tracker = temp_pipeline.Tracker()
    for i in range(8):
        tracker.upsert_job(JobPosting(portal="linkedin", external_id=str(i), title=f"Dev {i}", company=f"Co {i}",
                                      url="u"), JobStatus.APPROVED)
    settings = temp_pipeline.get_settings()
    settings = settings.model_copy(update={"portals": {**settings.portals,
                                                       "linkedin": PortalSettings(daily_cap=5)}})
    plan = temp_pipeline._plan({}, {"configurable": {"tracker": tracker, "settings": settings, "max_count": None}})
    assert [len(batch) for name, batch in plan["batches"] if name == "linkedin"] == [5]


def test_undated_postings_pass_only_when_the_portal_trusts_its_search_filter(temp_pipeline):
    from types import SimpleNamespace

    from job_copilot.models import JobPosting, PortalSettings

    tracker, stats = temp_pipeline.Tracker(), {"stale": 0, "undated": 0}
    settings = temp_pipeline.get_settings()
    undated = JobPosting(portal="indeed", external_id="1", title="Dev", company="X", url="u")
    trusting = SimpleNamespace(settings=PortalSettings(trust_search_age_filter=True))
    assert temp_pipeline._fresh_enough(undated, tracker, stats, settings, trusting) is True
    strict = SimpleNamespace(settings=PortalSettings())
    assert temp_pipeline._fresh_enough(undated, tracker, stats, settings, strict) is (not settings.skip_if_date_unknown)


def test_discover_many_runs_portals_in_parallel_and_survives_one_failing(temp_pipeline, monkeypatch):
    import asyncio

    started, calls = [], []

    async def fake_discover(source, limit=None, rescore_first=True):
        started.append(source)
        calls.append(rescore_first)
        await asyncio.sleep(0.05)  # both must have started before either finishes
        assert len(started) == 2
        if source == "indeed":
            raise RuntimeError("Indeed blocked")
        return {"found": 3}

    async def fake_rescore():
        return {"rescored": 1}

    monkeypatch.setattr(temp_pipeline, "discover", fake_discover)
    monkeypatch.setattr(temp_pipeline, "rescore", fake_rescore)
    out = asyncio.run(temp_pipeline.discover_many(["naukri", "indeed"]))
    assert out["rescored"] == 1 and out["naukri"] == {"found": 3}
    assert "Indeed blocked" in out["indeed"]["error"]
    assert calls == [False, False]  # re-scored once up front, not once per portal


def test_parallel_apply_shares_the_daily_cap_between_portals(temp_pipeline):
    from job_copilot.models import JobPosting, JobStatus

    tracker = temp_pipeline.Tracker()
    for portal in ("naukri", "indeed"):
        for i in range(3):
            tracker.upsert_job(JobPosting(portal=portal, external_id=str(i), title=f"Dev {portal} {i}",
                                          company=f"Co {portal} {i}", url="u"), JobStatus.APPROVED)
    config = {"configurable": {"tracker": tracker, "settings": temp_pipeline.get_settings(), "max_count": 4}}
    plan = temp_pipeline._plan({}, config)
    assert sum(len(b) for _, b in plan["batches"]) == 4  # not 3 + 3: each portal can't count down in parallel
    assert {name for name, _ in plan["batches"]} == {"naukri", "indeed"}


def test_chrome_is_not_quit_under_a_page_the_user_opened():
    from job_copilot.portals.browser import user_pages

    class Page:
        def __init__(self, url, closed=False):
            self.url, self.closed = url, closed

        def is_closed(self):
            return self.closed

    assert user_pages([Page("about:blank"), Page("https://x", closed=True)]) == []  # only the start tab is left
    mine = Page("https://www.naukri.com/job-1")
    assert user_pages([Page("about:blank"), mine]) == [mine]


def test_live_view_keeps_one_page_per_portal():
    from job_copilot.portals import live_view

    class FakePage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

    naukri, indeed = FakePage(), FakePage()
    live_view.set_page("naukri", naukri)
    live_view.set_page("indeed", indeed)
    try:
        assert live_view.current_page("naukri") is naukri and live_view.current_page("indeed") is indeed
        assert sorted(live_view.active_portals()) == ["indeed", "naukri"]
        indeed.closed = True
        assert live_view.active_portals() == ["naukri"] and not live_view.is_active("indeed")
    finally:
        live_view.set_page("naukri", None)
        live_view.set_page("indeed", None)
    assert not live_view.is_active()


def test_apply_graph_with_nothing_approved(temp_pipeline):
    import asyncio

    out = asyncio.run(temp_pipeline.apply_approved())
    assert out["attempted"] == 0 and out["cap_reached"] is False


# ---------- computer-use agent (fake model, fake page: offline) ----------

class FakePage:
    def __init__(self):
        self.viewport_size = {"width": 1000, "height": 500}
        self.url = "https://www.naukri.com/job"
        self.events: list[tuple] = []
        outer = self

        class Mouse:
            async def click(self, x, y, **kw): outer.events.append(("click", x, y, kw.get("click_count", 1)))
            async def move(self, x, y): outer.events.append(("move", x, y))
            async def wheel(self, dx, dy): outer.events.append(("wheel", dx, dy))

        class Keyboard:
            async def type(self, text): outer.events.append(("type", text))
            async def press(self, key): outer.events.append(("press", key))

        self.mouse, self.keyboard = Mouse(), Keyboard()

    async def wait_for_timeout(self, ms): pass
    async def screenshot(self, **kw): return b"png"


def _call(name, call_id, **args):
    from types import SimpleNamespace
    return SimpleNamespace(type="function_call", name=name, id=call_id, arguments=args)


class FakeModel:
    """Plays back scripted turns; records what the agent sent back."""

    def __init__(self, turns):
        from types import SimpleNamespace
        self.turns, self.sent = list(turns), []
        self.aio = SimpleNamespace(interactions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        from types import SimpleNamespace
        self.sent.append(kw["input"])
        steps = self.turns.pop(0) if self.turns else []
        return SimpleNamespace(id=f"i{len(self.sent)}", steps=steps)


def _text(msg):
    from types import SimpleNamespace
    return SimpleNamespace(type="model_output", text=msg, content=None)


def _run_cua(turns, lookup=lambda q, o: "30", max_steps=10):
    import asyncio
    from job_copilot.agents.cua import run_cua

    page, model = FakePage(), FakeModel(turns)
    return page, model, asyncio.run(run_cua(page, "task", lookup, max_steps, client=model, model="m"))


def test_cua_happy_path_scales_coordinates_and_records_answers():
    page, _, res = _run_cua([
        [_call("lookup_answer", "a", question="Notice period?", options=[])],
        [_call("click", "b", x=500, y=500)],
        [_call("type", "c", text="30", press_enter=False)],
        [_text("DONE")],
    ])
    assert res.done and res.answered == ["Notice period? = 30"]
    assert ("click", 500, 250, 1) in page.events and ("type", "30") in page.events


def test_cua_refuses_to_type_text_the_lookup_did_not_return():
    page, model, res = _run_cua([
        [_call("type", "a", text="1000000")],
        [_text("STOP: unsure")],
    ])
    assert ("type", "1000000") not in page.events
    assert "refused" in str(model.sent[1])
    assert not res.done


def test_cua_stops_when_there_is_no_verified_answer():
    _, _, res = _run_cua([
        [_call("lookup_answer", "a", question="Years of Cobol?", options=["1", "2"])],
        [_text("STOP: no verified answer for: Years of Cobol?")],
    ], lookup=lambda q, o: None)
    assert not res.done and "no verified answer" in res.reason and res.answered == []


def test_cua_stops_on_safety_confirmation_and_step_cap_and_foreign_host():
    _, _, res = _run_cua([[_call("click", "a", x=1, y=1,
                                 safety_decision={"decision": "require_confirmation", "explanation": "submit"})]])
    assert not res.done and "confirmation" in res.reason

    endless = [[_call("wait", str(i), seconds=1)] for i in range(10)]
    _, _, res = _run_cua(endless, max_steps=3)
    assert not res.done and "step limit" in res.reason

    import asyncio
    from job_copilot.agents.cua import run_cua
    page, model = FakePage(), FakeModel([[_call("click", "a", x=1, y=1)]])
    page.url = "https://evil.example.com/x"
    res = asyncio.run(run_cua(page, "task", lambda q, o: None, 5, client=model, model="m"))
    assert not res.done and "left naukri.com" in res.reason


# ---------- Groq / Ollama checks (mock HTTP: offline) ----------

def test_ollama_lists_downloaded_models(monkeypatch):
    import httpx
    from job_copilot.llm import providers

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    handler = lambda req: httpx.Response(200, json={"models": [{"name": "llama3.2:3b", "size": 2_019_393_189}]})
    models = providers.ollama_models(httpx.Client(transport=httpx.MockTransport(handler)))
    assert [(m.name, m.size_gb) for m in models] == [("llama3.2:3b", 2.0)]


def test_groq_key_validation(monkeypatch):
    import httpx
    from job_copilot.llm import providers

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        providers.groq_models()
    monkeypatch.setenv("GROQ_API_KEY", "k")
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})))
    assert providers.groq_models(ok) == ["a", "b"]
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(httpx.HTTPStatusError):
        providers.groq_models(bad)


# ---------- LLM provider selection and chat client (mock HTTP: offline) ----------

def _clear_llm_env(monkeypatch):
    for k in ("LLM_PROVIDER", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GROQ_API_KEY", "OLLAMA_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_llm_auto_selection_order(monkeypatch):
    from job_copilot.llm.factory import get_llm

    _clear_llm_env(monkeypatch)
    assert get_llm() is None
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    assert get_llm().label == "Ollama"
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert get_llm().label == "Groq"
    monkeypatch.setenv("LLM_PROVIDER", "none")
    assert get_llm() is None
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert get_llm().label == "Ollama"


def test_llm_explicit_provider_not_configured_falls_back(monkeypatch):
    from job_copilot.llm.factory import get_llm

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert get_llm() is None
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        get_llm()


def test_chat_llm_json_retries_once_on_invalid_output():
    import httpx
    from pydantic import BaseModel
    from job_copilot.llm.chat import ChatCompletionsLLM

    class Out(BaseModel):
        n: int

    replies = iter(['{"n": "not a number"}', '{"n": 3}'])
    seen = []

    def handler(req):
        seen.append(req.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    llm = ChatCompletionsLLM("T", "http://x/v1", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert llm.generate_json("p", Out, system="s") == Out(n=3)
    assert len(seen) == 2 and b"json_object" in seen[0]


def test_chat_llm_honours_retry_after_and_email_failure_is_not_fatal(monkeypatch):
    import smtplib
    import httpx
    from job_copilot.agents import emailer
    from job_copilot.llm import chat
    from job_copilot.llm.chat import ChatCompletionsLLM

    waits = []
    monkeypatch.setattr(chat.time, "sleep", waits.append)
    replies = iter([httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(
        200, json={"choices": [{"message": {"content": "hi"}}]})])
    llm = ChatCompletionsLLM("T", "http://x/v1", "m", client=httpx.Client(transport=httpx.MockTransport(lambda r: next(replies))))
    assert llm.generate_text("p") == "hi" and waits == [3.5]

    class Boom:
        def __init__(self, *a, **k): raise smtplib.SMTPServerDisconnected("closed")

    monkeypatch.setattr(emailer.smtplib, "SMTP", Boom)
    cfg = emailer.EmailConfig(host="h", port=587, user="u", password="p", to="t")
    assert emailer.send_email("s", "b", cfg=cfg) is False


def test_gemini_backend_selection(monkeypatch, tmp_path):
    from job_copilot.llm import gemini

    key = tmp_path / "sa.json"
    key.write_text("{}")
    for k in ("GEMINI_BACKEND", "GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert not gemini.use_vertex() and not gemini.gemini_available()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert gemini.gemini_available() and not gemini.use_vertex()
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
    assert gemini.use_vertex() and gemini.gemini_available()          # auto prefers Vertex
    monkeypatch.setenv("GEMINI_BACKEND", "api_key")
    assert not gemini.use_vertex()
    monkeypatch.setenv("GEMINI_BACKEND", "bogus")
    with pytest.raises(ValueError):
        gemini.use_vertex()


# ---------------------------------------------------------------- email report sending

class FakeSMTP:
    """Records what the emailer does instead of opening a network connection."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls, self.sent = host, port, [], None
        FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): self.calls.append("starttls")
    def login(self, user, password): self.calls.append(("login", user, password))
    def send_message(self, msg): self.sent = msg


def test_send_email_builds_message_with_attachment(monkeypatch, tmp_path):
    from job_copilot.agents import emailer

    FakeSMTP.instances.clear()
    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"xlsx-bytes")
    cfg = emailer.EmailConfig(host="smtp.test", port=587, user="me@test", password="pw", to="you@test")

    assert emailer.send_email("Daily", "body text", [report], cfg=cfg) is True

    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.test", 587)
    assert smtp.calls == ["starttls", ("login", "me@test", "pw")]    # TLS before credentials
    msg = smtp.sent
    assert (msg["From"], msg["To"], msg["Subject"]) == ("me@test", "you@test", "Daily")
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "report.xlsx" and attachment.get_content() == b"xlsx-bytes"


def test_send_email_to_override_and_unconfigured_skips(monkeypatch):
    from job_copilot.agents import emailer

    FakeSMTP.instances.clear()
    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    cfg = emailer.EmailConfig(host="h", port=587, user="u", password="p", to="default@test")
    assert emailer.send_email("s", "b", cfg=cfg, to="other@test") is True
    assert FakeSMTP.instances[0].sent["To"] == "other@test"

    FakeSMTP.instances.clear()
    empty = emailer.EmailConfig(host="h", port=587, user="", password="", to="")
    assert emailer.send_email("s", "b", cfg=empty) is False
    assert FakeSMTP.instances == []                                  # never even connects


def test_daily_report_emails_only_when_enabled(temp_pipeline, monkeypatch, tmp_path):
    from types import SimpleNamespace

    pipeline = temp_pipeline
    report = tmp_path / "r.xlsx"
    report.write_bytes(b"x")
    sent = []
    monkeypatch.setattr(pipeline.reporter, "build_report", lambda jobs: report)
    monkeypatch.setattr(pipeline.emailer, "send_email", lambda subject, body, files: sent.append((subject, files)) or True)

    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(send_daily_report=True))
    out = pipeline.daily_report(send=True)
    assert out["emailed"] is True and sent[0][1] == [report] and sent[0][0].startswith("Job Copilot daily report")

    sent.clear()
    assert pipeline.daily_report(send=False)["emailed"] is False     # --no-email
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(send_daily_report=False))
    assert pipeline.daily_report(send=True)["emailed"] is False      # disabled in settings
    assert sent == []


# ---------------------------------------------------------------- PII redaction

def _pii_profile():
    from job_copilot.models import Personal, Preferences, Profile, Project

    return Profile(
        personal=Personal(name="Asha Rao", email="asha.rao@example.com", phone="+91 98765 43210",
                          location="Koramangala, Bengaluru, India", linkedin="https://www.linkedin.com/in/asha-rao",
                          github="https://github.com/asharao", portfolio="asharao.dev"),
        summary="Backend engineer.", total_experience_years=3, skills=[], preferences=Preferences(roles=["Dev"]),
        projects=[Project(id="shop", name="Shop", github="https://github.com/asharao/shop", description="API.")],
    )


PII_VALUES = ["Asha", "Rao", "asha.rao@example.com", "98765", "43210", "Koramangala", "Bengaluru", "asharao"]


class _EchoLLM:
    """Fake provider: records what it was sent and echoes the masked prompt back in its reply."""

    label = "Fake"

    def __init__(self):
        self.sent: list[str] = []

    def generate_text(self, prompt, system=None, temperature=0.4):
        self.sent += [prompt, system or ""]
        return prompt

    def generate_json(self, prompt, schema, system=None, temperature=0.2):
        self.sent += [prompt, system or ""]
        return schema(summary=prompt, skills=[], projects=[], keywords_used=[])


def test_redactor_masks_profile_values_and_generic_contacts():
    from job_copilot.llm.redact import Redactor, profile_pii

    r = Redactor(profile_pii(_pii_profile()))
    text = ("ASHA RAO, Koramangala, Bengaluru. Mail asha.rao@example.com or 9876543210. "
            "Code: github.com/asharao/shop, https://www.linkedin.com/in/asha-rao/. Site: asharao.dev. "
            "Recruiter: hr@acme.io, +1 (415) 555-0134. Worked 2018 - 2022 at 12 LPA; Asha led it.")
    masked = r.mask(text)
    assert not any(v.lower() in masked.lower() for v in PII_VALUES + ["hr@acme.io", "555-0134"])
    assert "github.com/[URL_" in masked and "/shop" in masked  # host and repo kept so the model can tell links apart
    assert "2018 - 2022" in masked and "12 LPA" in masked  # years and salary are not phone numbers
    assert r.unmask(masked) == text


def test_redacting_llm_never_sends_pii_and_restores_replies():
    from job_copilot.llm.redact import RedactingLLM, profile_pii
    from job_copilot.models import TailoredContent

    fake = _EchoLLM()
    llm = RedactingLLM(fake, lambda: profile_pii(_pii_profile()))
    prompt = "Tailor for Asha Rao (asha.rao@example.com, https://github.com/asharao/shop)"
    assert llm.generate_text(prompt, system="Contact +91-98765-43210") == prompt
    assert llm.generate_json(prompt, TailoredContent).summary == prompt
    assert not any(v.lower() in s.lower() for s in fake.sent for v in PII_VALUES)


def test_redacting_llm_rejects_invented_placeholders():
    from job_copilot.llm.redact import Redactor

    with pytest.raises(ValueError, match="never sent"):
        Redactor({}).unmask("Contact [EMAIL_7]")


def test_import_masks_the_resume_name_before_the_profile_has_it():
    from job_copilot.agents.profile_importer import extract_profile, name_guess
    from job_copilot.llm.redact import RedactingLLM

    assert name_guess("\n  Meera Iyer \nBackend Engineer") == "Meera Iyer"
    assert name_guess("meera@example.com | +91 9000000000") == ""
    sent = []

    class Fake:
        def generate_json(self, prompt, schema, system=None, temperature=0.2):
            sent.append(prompt)
            return _extract([]).model_copy(update={"personal": Personal(name="[NAME_1]", email="[EMAIL_1]")})

    from job_copilot.models import Personal

    out = extract_profile("Meera Iyer\nmeera@example.com\nPython", RedactingLLM(Fake(), lambda: {}))
    assert "Meera" not in sent[0] and "meera@" not in sent[0]
    assert (out.personal.name, out.personal.email) == ("Meera Iyer", "meera@example.com")


def test_mcp_profile_has_no_contact_details(monkeypatch):
    from job_copilot import mcp_server

    monkeypatch.setattr(mcp_server, "get_profile", _pii_profile)
    for out in (str(mcp_server.get_profile_summary()), mcp_server.profile_yaml()):
        assert "personal" not in out and "asharao" not in out and "Asha" not in out


def test_get_llm_wraps_every_provider_when_redaction_is_on(monkeypatch):
    from job_copilot.llm.factory import get_llm
    from job_copilot.llm.redact import RedactingLLM

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    assert isinstance(get_llm(), RedactingLLM)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert isinstance(get_llm(), RedactingLLM) and get_llm().label == "Groq"
