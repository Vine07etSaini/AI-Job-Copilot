from datetime import datetime, timedelta

import pytest

from job_copilot.agents.freshness import is_fresh, parse_posted
from job_copilot.agents.honesty_guard import guard
from job_copilot.agents.matcher import score_job
from job_copilot.agents.screening import answer_question
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import IST, get_profile
from job_copilot.models import JDAnalysis, ProjectRewrite, ScoreWeights, SkillRequirement, TailoredContent

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


@pytest.fixture
def profile():
    return get_profile()


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
        role_title="x",
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
    handled = sum(stats[k] for k in ("filtered_out", "pending_review", "approved", "stale", "undated", "duplicate"))
    assert handled == stats["found"]
    # a second run sees everything as already known: the loop terminates and de-dupes
    again = asyncio.run(temp_pipeline.discover("sample"))
    assert again["duplicate"] == again["found"] and again["pending_review"] == 0


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
