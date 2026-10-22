"""Naukri.com via Playwright with a persistent, logged-in browser profile.

The session is saved and reused. When it has expired, the portal signs in automatically with
NAUKRI_EMAIL / NAUKRI_PASSWORD from .env; without them (or on OTP / captcha) run
`python main.py login naukri` to sign in by hand.
Naukri changes its markup often, so selectors are kept in one place with fallbacks.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

from playwright.async_api import TimeoutError as PWTimeout

from job_copilot.agents.freshness import parse_posted
from job_copilot.agents.cua import run_cua
from job_copilot.agents.screening import answer_question
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import PortalCredentials, STORAGE_DIR, get_portal_credentials, get_profile, get_settings
from job_copilot.llm.gemini import gemini_available
from job_copilot.models import JobPosting, JobStatus
from job_copilot.portals.browser import ChromePortal

log = logging.getLogger(__name__)

BASE = "https://www.naukri.com"
LOGIN_URL = f"{BASE}/nlogin/login"
LOGIN_USER = "#usernameField, input[placeholder*='Email' i], input[type='email']"
LOGIN_PASSWORD = "#passwordField, input[type='password']"
LOGIN_SUBMIT = "button[type='submit'], button:has-text('Login')"
LOGIN_OTP = "input[placeholder*='OTP' i], [class*='otp' i] input"
LOGIN_TO_APPLY = "#login-apply-button, button:has-text('Login to apply')"
# The recruiter's screening questions open as a chatbot drawer that slides in from the right.
DRAWER = ".chatbot_Drawer"
DRAWER_BOT_MESSAGE = ".chatbot_Drawer .botItem .botMsg"
DRAWER_OPTION_LABEL = ".chatbot_Drawer label.ssrc__label"
DRAWER_TEXT_BOX = ".chatbot_Drawer .chatbot_SendMessageContainer:not(.d-none) [contenteditable='true']"
DRAWER_SAVE = ".chatbot_Drawer .sendMsg"
MAX_SCREENING_QUESTIONS = 15

CARDS_JS = """
() => Array.from(document.querySelectorAll('.srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple'))
  .map(el => {
    const txt = s => (el.querySelector(s)?.innerText || '').trim();
    const a = el.querySelector('a.title');
    return {
      id: el.getAttribute('data-job-id') || '',
      title: a ? a.innerText.trim() : '',
      url: a ? a.href : '',
      company: txt('a.comp-name, .comp-name'),
      exp: txt('.expwdth, .exp-wrap, .exp'),
      salary: txt('.sal-wrap, .sal'),
      location: txt('.locWdth, .loc-wrap, .loc'),
      posted: txt('.job-post-day, [class*="post-day"], .postedDate'),
      snippet: txt('.job-desc, [class*="job-desc"]'),
      tags: Array.from(el.querySelectorAll('ul.tags-gt li, .tags li')).map(li => li.innerText.trim()).filter(Boolean),
    };
  })
"""

DETAILS_JS = """
() => {
  const pick = sels => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el && el.innerText.trim().length > 50) return el.innerText.trim();
    }
    return '';
  };
  const stats = Array.from(document.querySelectorAll('[class*="jhc__stat"], [class*="stat"]'))
    .map(e => e.innerText.trim());
  const posted = (stats.find(t => /^posted/i.test(t)) || '').replace(/^posted\\s*:?\\s*/i, '');
  const skills = Array.from(document.querySelectorAll('[class*="key-skill"] a, [class*="key-skill"] span'))
    .map(e => e.innerText.trim()).filter(Boolean);
  return {
    description: pick(['[class*="dang-inner-html"]', '[class*="job-desc"]', 'section.job-desc', '[class*="JDC"]']),
    posted, skills,
  };
}
"""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def search_url(role: str, location: str, page: int, max_age_days: int, experience_years: float) -> str:
    suffix = f"-{page}" if page > 1 else ""
    params = {"jobAge": max_age_days, "experience": int(experience_years)}
    if not location or location.lower() == "remote":
        if location:
            params["wfhType"] = 2
        path = f"{_slug(role)}-jobs{suffix}"
    else:
        path = f"{_slug(role)}-jobs-in-{_slug(location)}{suffix}"
    return f"{BASE}/{path}?{urlencode(params)}"


class NaukriPortal(ChromePortal):
    name = "naukri"
    label = "Naukri"
    login_url = LOGIN_URL

    # ------------------------------------------------------------ search

    async def search(self, role: str, location: str, max_age_days: int, experience_years: float,
                     target_cards: int = 0) -> list[JobPosting]:
        results: list[JobPosting] = []
        seen: set[str] = set()
        pages_done, new_on_page = 0, 0
        while pages_done == 0 or self.keep_paging(pages_done, new_on_page, len(results), target_cards):
            if pages_done:
                await self.pause()
            pages_done += 1
            url = search_url(role, location, pages_done, max_age_days, experience_years)
            log.info("Naukri search: %s", url)
            await self._goto(url)
            try:
                await self.page.wait_for_selector(".srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple", timeout=15000)
            except PWTimeout:
                log.info("No job cards on %s", url)
                break
            new_on_page = 0
            for c in await self.page.evaluate(CARDS_JS):
                if not c["url"] or not c["title"]:
                    continue
                ext_id = c["id"] or (m.group(1) if (m := re.search(r"-(\d{6,})(?:[?#]|$)", c["url"])) else c["url"])
                if ext_id in seen:
                    continue
                seen.add(ext_id)
                new_on_page += 1
                results.append(JobPosting(
                    portal=self.name, external_id=ext_id, title=c["title"], company=c["company"] or "Unknown",
                    url=c["url"].split("?")[0], location=c["location"], experience_text=c["exp"],
                    salary_text=c["salary"], posted_text=c["posted"], posted_at=parse_posted(c["posted"]),
                    tags=c["tags"], snippet=c["snippet"],
                ))
            log.info("Naukri page %d: %d new cards (%d collected)", pages_done, new_on_page, len(results))
        return results

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        await self._goto(posting.url)
        try:
            await self.page.wait_for_selector('[class*="dang-inner-html"], [class*="job-desc"]', timeout=15000)
        except PWTimeout:
            log.warning("Description not found for %s", posting.url)
        d = await self.page.evaluate(DETAILS_JS)
        updated = posting.model_copy()
        updated.description = d["description"] or posting.description
        if d["posted"]:
            # second freshness check: the job page's own date wins over the search card
            updated.posted_text = d["posted"]
            updated.posted_at = parse_posted(d["posted"]) or posting.posted_at
        if d["skills"]:
            updated.tags = list(dict.fromkeys([*posting.tags, *d["skills"]]))
        await self.pause()
        return updated

    # ------------------------------------------------------------ login

    async def login(self, creds: PortalCredentials | None = None) -> tuple[bool, str]:
        """Sign in with `creds` (else the .env credentials); tried once per session so a bad password can't lock the account."""
        if self._login_attempted:
            return False, "automatic login was already tried in this session"
        self._login_attempted = True
        creds = creds or get_portal_credentials(self.name)
        if not creds.configured:
            return False, "set NAUKRI_EMAIL and NAUKRI_PASSWORD in .env, or run: python main.py login naukri"

        await self._goto(LOGIN_URL)
        user = self.page.locator(LOGIN_USER).first
        try:
            await user.wait_for(timeout=15000)
        except PWTimeout:
            if "nlogin" not in self.page.url:
                return True, "already logged in"  # Naukri redirects signed-in users away from the login page
            return False, "login form not found - run: python main.py login naukri"
        await user.fill("")
        await user.press_sequentially(creds.user, delay=60)
        await self.page.locator(LOGIN_PASSWORD).first.press_sequentially(creds.password, delay=60)
        await self.page.locator(LOGIN_SUBMIT).first.click()
        try:
            await self.page.wait_for_url(lambda u: "nlogin" not in u, timeout=20000)
        except PWTimeout:
            if await self._visible(LOGIN_OTP):
                return False, "Naukri asked for an OTP - run: python main.py login naukri"
            return False, "login did not complete (wrong credentials or captcha?) - run: python main.py login naukri"
        log.info("Logged in to Naukri as %s", creds.user)
        await self.pause()
        return True, "logged in"

    def on_login_page(self, url: str) -> bool:
        return "nlogin" in url

    async def is_logged_in(self) -> bool:
        """The home page title says 'Mynaukri' only for a signed-in user."""
        await self._goto(BASE)
        await self.page.wait_for_load_state("load")
        return "mynaukri" in (await self.page.title()).lower()

    # ------------------------------------------------------------ apply

    async def apply(self, url: str) -> tuple[JobStatus, str]:
        await self._goto(url)
        await self.page.wait_for_timeout(2500)
        if await self._visible("text=/already applied/i"):
            return JobStatus.APPLIED, "already applied"
        if await self._visible("#company-site-button, button:has-text('Apply on company site')"):
            return JobStatus.NEEDS_MANUAL, "applies on company site"
        if await self._visible(LOGIN_TO_APPLY):
            ok, reason = await self.login()
            if not ok:
                return JobStatus.NEEDS_MANUAL, f"not logged in: {reason}"
            return await self.apply(url)  # login() runs once per session, so this can't loop
        button = self.page.locator("#apply-button, button.apply-button, button:has-text('Apply')").first
        try:
            await button.click(timeout=5000)
        except PWTimeout:
            return JobStatus.NEEDS_MANUAL, "apply button not found"
        await self.page.wait_for_timeout(4000)
        answered: list[str] = []
        if await self._visible(DRAWER):
            if reason := await self._handle_drawer(url, answered):
                return JobStatus.NEEDS_MANUAL, f"screening questions - {reason}; finish in the browser"
        if await self._visible("text=/successfully applied|applied to|you have applied/i"):
            await self.pause()
            return JobStatus.APPLIED, "applied on Naukri" + (f" (answered: {'; '.join(answered)})" if answered else "")
        return JobStatus.NEEDS_MANUAL, "could not confirm the application - please check"

    async def _handle_drawer(self, url: str, answered: list[str]) -> str | None:
        """Answer the drawer with the computer-use agent when enabled and available, else with the scripted rules."""
        settings = get_settings()
        if settings.apply_engine != "cua" or not gemini_available():
            return await self._answer_screening(answered)
        profile = get_profile()
        bank = SkillBank(profile)
        result = await run_cua(
            self.page, "Answer the recruiter's screening questions in the open drawer and save each answer.",
            lambda question, options: answer_question(question, options, profile, bank), settings.cua_max_steps,
            shots_dir=STORAGE_DIR / "cua" / _slug(url)[-60:])
        answered.extend(result.answered)
        log.info("CUA finished: done=%s steps=%d %s", result.done, result.steps, result.reason)
        return None if result.done else result.reason or "agent stopped"

    async def _answer_screening(self, answered: list[str]) -> str | None:
        """Answer the right-hand questions drawer from the profile. Returns why it stopped, or None when it closed."""
        profile = get_profile()
        bank = SkillBank(profile)
        previous = ""
        for _ in range(MAX_SCREENING_QUESTIONS):
            messages = await self.page.locator(DRAWER_BOT_MESSAGE).all_inner_texts()
            question = messages[-1].strip() if messages else ""
            if not question or question == previous:
                return f"no new question after answering '{previous}'" if previous else "question not readable"
            options = [o.strip() for o in await self.page.locator(DRAWER_OPTION_LABEL).all_inner_texts()]
            text_box = self.page.locator(DRAWER_TEXT_BOX)
            if not options and not await text_box.is_visible():
                return f"unsupported question type: '{question}'"
            answer = answer_question(question, options, profile, bank)
            if answer is None:
                return f"no verified answer for '{question}'"
            if options:
                exact = re.compile(rf"^\s*{re.escape(answer)}\s*$", re.I)
                await self.page.locator(DRAWER_OPTION_LABEL, has_text=exact).first.click()
            else:
                await text_box.fill(answer)
            log.info("Screening: %s -> %s", question, answer)
            answered.append(f"{question} = {answer}")
            previous = question
            await self.page.locator(DRAWER_SAVE).click()
            await self.page.wait_for_timeout(2500)
            if not await self._visible(DRAWER):
                return None
        return "too many questions"
