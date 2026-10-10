"""Naukri.com via Playwright with a persistent, logged-in browser profile.

The session is saved and reused. When it has expired, the portal signs in automatically with
NAUKRI_EMAIL / NAUKRI_PASSWORD from .env; without them (or on OTP / captcha) run
`python main.py login naukri` to sign in by hand.
Naukri changes its markup often, so selectors are kept in one place with fallbacks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlencode

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from job_copilot.agents.freshness import parse_posted
from job_copilot.agents.cua import run_cua
from job_copilot.agents.screening import answer_question
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import PortalCredentials, STORAGE_DIR, get_portal_credentials, get_profile, get_settings
from job_copilot.llm.gemini import gemini_available
from job_copilot.models import JobPosting, JobStatus, PortalSettings
from job_copilot.portals import live_view
from job_copilot.portals.base import Portal, PortalBlocked
from job_copilot.portals.chrome_debug import ensure_chrome, hide_from_taskbar, keep_awake

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


class NaukriPortal(Portal):
    name = "naukri"

    async def open(self) -> None:
        """Attach to a normal Chrome over CDP (started on demand); it is a real, visible browser, not a launched one."""
        self._login_attempted = False
        cdp_url = self.settings.cdp_url
        # Blocking subprocess/HTTP polling, so keep it off the event loop.
        await asyncio.to_thread(ensure_chrome, cdp_url)
        keep_awake(True)  # same thread (the event loop) as close(), which the API requires
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.connect_over_cdp(cdp_url)
        if self.browser.contexts:
            self.ctx: BrowserContext = self.browser.contexts[0]  # the default context holds the saved Naukri login
        else:  # unexpected Chrome state: a fresh context has no saved login, so say so instead of failing with IndexError
            log.warning("Chrome exposed no default browser context; using a new one (the saved Naukri login will not apply)")
            self.ctx = await self.browser.new_context()
        # Our own tab, so the user's other tabs are left alone.
        self.page: Page = await self.ctx.new_page()
        hide_from_taskbar(cdp_url)  # new windows get their own taskbar button, so do it after opening our tab
        live_view.set_page(self.page)

    async def close(self) -> None:
        """Close our tab, then the debug Chrome itself unless `close_chrome_on_finish` is off (it restarts on demand)."""
        live_view.set_page(None)
        keep_awake(False)
        if getattr(self, "page", None) and not self.page.is_closed():
            await self.page.close()
        if self.settings.close_chrome_on_finish and getattr(self, "browser", None):
            try:  # clean shutdown through CDP, so the profile (login cookies) is flushed to disk
                await (await self.browser.new_browser_cdp_session()).send("Browser.close")
            except Exception as e:  # already gone or refused: nothing more to do
                log.debug("Could not close Chrome: %s", e)
        if getattr(self, "_pw", None):
            await self._pw.stop()  # disconnects from Chrome

    async def _goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        title = (await self.page.title() or "").lower()
        if "access denied" in title or "captcha" in title:
            raise PortalBlocked(f"Naukri blocked the request ({title}). Lower the volume and retry later.")

    # ------------------------------------------------------------ search

    async def search(self, role: str, location: str, max_age_days: int, experience_years: float) -> list[JobPosting]:
        results: list[JobPosting] = []
        for page_no in range(1, self.settings.pages_per_search + 1):
            url = search_url(role, location, page_no, max_age_days, experience_years)
            log.info("Naukri search: %s", url)
            await self._goto(url)
            try:
                await self.page.wait_for_selector(".srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple", timeout=15000)
            except PWTimeout:
                log.info("No job cards on %s", url)
                break
            cards = await self.page.evaluate(CARDS_JS)
            for c in cards:
                if not c["url"] or not c["title"]:
                    continue
                ext_id = c["id"] or (m.group(1) if (m := re.search(r"-(\d{6,})(?:[?#]|$)", c["url"])) else c["url"])
                results.append(JobPosting(
                    portal=self.name, external_id=ext_id, title=c["title"], company=c["company"] or "Unknown",
                    url=c["url"].split("?")[0], location=c["location"], experience_text=c["exp"],
                    salary_text=c["salary"], posted_text=c["posted"], posted_at=parse_posted(c["posted"]),
                    tags=c["tags"],
                ))
            if len(cards) < 15:
                break
            await self.pause()
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

    async def is_logged_in(self) -> bool:
        """The home page title says 'Mynaukri' only for a signed-in user."""
        await self._goto(BASE)
        await self.page.wait_for_load_state("load")
        return "mynaukri" in (await self.page.title()).lower()

    # ------------------------------------------------------------ apply

    async def _visible(self, selector: str) -> bool:
        try:
            return await self.page.locator(selector).first.is_visible(timeout=1500)
        except PWTimeout:
            return False

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


async def login_session() -> str:
    """Sign in automatically if possible, else let the user sign in by hand; the session is saved in storage/browser."""
    async with NaukriPortal(PortalSettings()) as portal:
        try:
            ok, reason = await portal.login()
        except PortalBlocked as e:
            ok, reason = False, str(e)
        if ok:
            return f"automatic login: {reason}"
        log.warning("Automatic login not possible: %s", reason)
        if "nlogin" not in portal.page.url:
            await portal.page.goto(LOGIN_URL)
        await asyncio.to_thread(input, "Log in to Naukri in the opened browser window, then press Enter here... ")
        return "manual login"


async def interactive_login(creds: PortalCredentials | None, finished: asyncio.Event, on_state, timeout: int = 300) -> bool:
    """Open the login page for the user. Try `creds` once if given, else wait while the user signs in by hand
    (through the web UI's live view). `on_state(state, message)` reports progress; `finished` ends the wait early."""
    async with NaukriPortal(PortalSettings()) as portal:
        try:
            await portal._goto(LOGIN_URL)
            if "nlogin" not in portal.page.url:
                on_state("logged_in", "Already logged in.")
                return True
            if creds is not None:
                on_state("running", "Signing in with the details you entered...")
                ok, reason = await portal.login(creds)
                if ok:
                    on_state("logged_in", "Logged in.")
                    return True
                on_state("waiting", f"{reason}. Finish logging in using the live view (tick 'Take control').")
                if "nlogin" not in portal.page.url:
                    await portal._goto(LOGIN_URL)
            else:
                on_state("waiting", "Log in using the live view (tick 'Take control'), then press Done.")
            deadline = asyncio.get_running_loop().time() + timeout
            while "nlogin" in portal.page.url and not finished.is_set():
                if asyncio.get_running_loop().time() > deadline:
                    on_state("failed", "Timed out waiting for the login.")
                    return False
                await asyncio.sleep(2)
            if await portal.is_logged_in():
                on_state("logged_in", "Logged in. The session is saved.")
                return True
            on_state("failed", "Not logged in. Try again.")
            return False
        except PortalBlocked as e:
            on_state("failed", str(e))
            return False
