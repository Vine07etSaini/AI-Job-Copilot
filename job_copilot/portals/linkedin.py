"""LinkedIn via the shared debug Chrome (own window and live view, runs in parallel with the other portals).

LinkedIn is the strictest site about automation and can restrict accounts, so this portal is deliberately slow and
small: a separate daily cap (`portals.linkedin.daily_cap`), long pauses, search limited to "Easy Apply" jobs (the
only ones it applies to), and any security checkpoint stops the run. Sign in once with "Log in to LinkedIn" in the web
UI (LINKEDIN_EMAIL / LINKEDIN_PASSWORD in .env are tried once; verification codes go through the live view).
Selectors cover the signed-in pages and the public (guest) ones, with fallbacks; LinkedIn changes markup often.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urlparse

from playwright.async_api import TimeoutError as PWTimeout

from job_copilot.agents.freshness import parse_posted
from job_copilot.config import PortalCredentials, get_portal_credentials
from job_copilot.models import JobPosting, JobStatus
from job_copilot.portals.base import PortalBlocked
from job_copilot.portals.browser import ChromePortal
from job_copilot.portals.forms import answer_form, text_of, visible

log = logging.getLogger(__name__)

BASE = "https://www.linkedin.com"
LOGIN_URL = f"{BASE}/login"
LOGIN_PATHS = ("/login", "/uas/login", "/checkpoint", "/authwall", "/signup")
LOGIN_USER = "#username"
LOGIN_PASSWORD = "#password"
LOGIN_SUBMIT = "button[type='submit']"
SIGNED_IN = ".global-nav__me, img.global-nav__me-photo, [data-control-name='nav.settings']"
GUEST_SIGN_IN = "a.nav__button-secondary, a[href*='/login']:has-text('Sign in'), button:has-text('Sign in')"
RESULTS = "[data-occludable-job-id], [data-job-id], .job-search-card, .base-card"
DESCRIPTION = "#job-details, .jobs-description__content, .jobs-box__html-content, .show-more-less-html__markup"
ALREADY_APPLIED = ("text=/applied \\d+ \\w+ ago|application submitted|you applied/i, "
                   ".jobs-s-apply .artdeco-inline-feedback--success")
EASY_APPLY = "button.jobs-apply-button:has-text('Easy Apply'), button[aria-label*='Easy Apply']"
OTHER_APPLY = "button.jobs-apply-button, a.jobs-apply-button, button:has-text('Apply'), a:has-text('Apply on company')"
# Easy Apply opens a modal dialog with numbered steps.
MODAL = ".jobs-easy-apply-modal, [data-test-modal-id='easy-apply-modal'], div[role='dialog']:has(form)"
QUESTION_ITEM = ".jobs-easy-apply-form-section__grouping, [data-test-form-element], .fb-dash-form-element, fieldset"
NEXT = ("button[aria-label='Submit application'], button[aria-label='Review your application'], "
        "button[aria-label='Continue to next step']")
SUBMITTED = "text=/application was sent|application sent|your application was submitted/i"
DISMISS = "button[aria-label='Dismiss'], button[aria-label='Done']"
FOLLOW_COMPANY = "input#follow-company-checkbox, input[id*='follow-company']"
STEP_ERROR = ".artdeco-inline-feedback--error"
MAX_APPLY_STEPS = 12
POSTED_RE = re.compile(r"just now|today|\d+\s*(?:minute|hour|day|week|month)s?\s*ago", re.I)

# Signed-in cards carry data-job-id / data-occludable-job-id; public (guest) cards carry a jobPosting URN.
CARDS_JS = """
() => {
  const jobs = new Map();
  const nodes = document.querySelectorAll('[data-occludable-job-id], [data-job-id], .base-card[data-entity-urn], .job-search-card');
  for (const el of nodes) {
    const urn = el.getAttribute('data-entity-urn') || '';
    const id = el.getAttribute('data-occludable-job-id') || el.getAttribute('data-job-id') || (urn.match(/(\\d+)$/) || [])[1];
    if (!id) continue;
    const txt = s => (el.querySelector(s)?.innerText || '').trim().split('\\n')[0].trim();
    const full = s => (el.querySelector(s)?.innerText || '').trim();  // the snippet is multi-line
    const time = el.querySelector('time');
    const job = jobs.get(id) || {};
    jobs.set(id, {
      id,
      title: job.title || txt('.job-card-list__title, .job-card-container__link strong, a[class*="job-card"] strong, .base-search-card__title'),
      company: job.company || txt('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, .base-search-card__subtitle'),
      location: job.location || txt('.artdeco-entity-lockup__caption, .job-card-container__metadata-item, .job-search-card__location'),
      posted: job.posted || (time ? (time.getAttribute('datetime') || time.innerText || '').trim() : ''),
      snippet: job.snippet || full('.job-search-card__snippet, .job-card-container__job-insight-text'),
      easyApply: job.easyApply || /easy apply/i.test(el.innerText),
    });
  }
  return [...jobs.values()];
}
"""

DETAILS_JS = """
() => {
  const txt = s => (document.querySelector(s)?.innerText || '').trim();
  return {
    description: txt('#job-details') || txt('.jobs-description__content') || txt('.jobs-box__html-content')
      || txt('.show-more-less-html__markup') || txt('.description__text'),
    posted: txt('.job-details-jobs-unified-top-card__tertiary-description-container')
      || txt('.jobs-unified-top-card__posted-date') || txt('.posted-time-ago__text'),
    company: txt('.job-details-jobs-unified-top-card__company-name') || txt('.jobs-unified-top-card__company-name')
      || txt('.topcard__org-name-link'),
  };
}
"""

# The results list loads more cards as it scrolls; scroll its own container (or the page on the guest site).
SCROLL_JS = """
() => {
  const list = document.querySelector('.jobs-search-results-list, .scaffold-layout__list > div, .scaffold-layout__list');
  (list || document.scrollingElement).scrollBy(0, 800);
}
"""


def search_url(role: str, location: str, start: int, max_age_days: int, easy_apply_only: bool) -> str:
    """`f_TPR=r<seconds>` keeps postings from the last N days, `f_AL=true` only Easy Apply, `f_WT=2` remote."""
    params: dict = {"keywords": role, "f_TPR": f"r{max_age_days * 86400}"}
    if location.lower() == "remote":
        params["f_WT"] = 2
    elif location:
        params["location"] = location
    if easy_apply_only:
        params["f_AL"] = "true"
    if start:
        params["start"] = start
    return f"{BASE}/jobs/search/?{urlencode(params)}"


def clean_posted(text: str) -> str:
    """The date in "Bengaluru · Reposted 3 days ago · 50 applicants", or an ISO date from a <time datetime=...>."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (text or "").strip()):
        return text.strip()
    m = POSTED_RE.search(text or "")
    return m.group(0) if m else ""


class LinkedInPortal(ChromePortal):
    name = "linkedin"
    label = "LinkedIn"
    login_url = LOGIN_URL

    async def _goto(self, url: str) -> None:
        await super()._goto(url)
        if "/checkpoint/" in self.page.url:
            raise PortalBlocked("LinkedIn showed a security check: stopped. Finish it in the live view, retry later.")

    # ------------------------------------------------------------ search

    async def search(self, role: str, location: str, max_age_days: int, experience_years: float,
                     target_cards: int = 0) -> list[JobPosting]:
        results: list[JobPosting] = []
        seen: set[str] = set()
        pages_done, new_on_page = 0, 0
        while pages_done == 0 or self.keep_paging(pages_done, new_on_page, len(results), target_cards):
            if pages_done:
                await self.pause()
            url = search_url(role, location, pages_done * 25, max_age_days, self.settings.easy_apply_only)
            pages_done += 1
            log.info("LinkedIn search: %s", url)
            await self._goto(url)
            if self.on_login_page(self.page.url):
                raise PortalBlocked("LinkedIn wants you to sign in first - use 'Log in to LinkedIn' in the web UI")
            try:
                await self.page.wait_for_selector(RESULTS, timeout=15000)
            except PWTimeout:
                log.info("No job cards on %s", url)
                break
            for _ in range(8):  # cards further down only render once scrolled into view
                await self.page.evaluate(SCROLL_JS)
                await self.page.wait_for_timeout(600)
            cards = [c for c in await self.page.evaluate(CARDS_JS) if c["title"] and c["id"] not in seen]
            new_on_page = len(cards)
            for c in cards:
                seen.add(c["id"])
                posted = clean_posted(c["posted"])
                results.append(JobPosting(
                    portal=self.name, external_id=c["id"], title=c["title"], company=c["company"] or "Unknown",
                    url=f"{BASE}/jobs/view/{c['id']}/", location=c["location"], snippet=c["snippet"],
                    posted_text=posted, posted_at=parse_posted(posted),
                ))
            log.info("LinkedIn page %d: %d new cards (%d collected)", pages_done, new_on_page, len(results))
        return results

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        await self._goto(posting.url)
        try:
            await self.page.wait_for_selector(DESCRIPTION, timeout=15000)
        except PWTimeout:
            log.warning("Description not found for %s", posting.url)
        d = await self.page.evaluate(DETAILS_JS)
        updated = posting.model_copy()
        updated.description = d["description"] or posting.description
        if d["company"] and posting.company == "Unknown":
            updated.company = d["company"]
        if posted := clean_posted(d["posted"]):
            # second freshness check: the job page's own date wins over the search card
            updated.posted_text = posted
            updated.posted_at = parse_posted(posted) or posting.posted_at
        await self.pause()
        return updated

    # ------------------------------------------------------------ login

    def on_login_page(self, url: str) -> bool:
        return any(urlparse(url).path.startswith(p) for p in LOGIN_PATHS)

    async def is_logged_in(self) -> bool:
        """The feed redirects visitors to the login page; signed-in members get the "Me" menu."""
        await self.page.goto(f"{BASE}/feed/", wait_until="domcontentloaded", timeout=45000)
        await self.page.wait_for_timeout(2000)
        return not self.on_login_page(self.page.url) and await self._visible(SIGNED_IN, 5000)

    async def login(self, creds: PortalCredentials | None = None) -> tuple[bool, str]:
        """Sign in with `creds` (else LINKEDIN_EMAIL / LINKEDIN_PASSWORD); tried once per session."""
        if self._login_attempted:
            return False, "automatic login was already tried in this session"
        self._login_attempted = True
        creds = creds or get_portal_credentials(self.name)
        if not creds.configured:
            return False, "no LinkedIn credentials - log in through the live view"
        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        if not self.on_login_page(self.page.url):
            return True, "already logged in"
        user = self.page.locator(LOGIN_USER).first
        try:
            await user.wait_for(timeout=15000)
        except PWTimeout:
            return False, "LinkedIn login form not found"
        await user.fill("")
        await user.press_sequentially(creds.user, delay=70)
        await self.page.locator(LOGIN_PASSWORD).first.press_sequentially(creds.password, delay=70)
        await self.page.locator(LOGIN_SUBMIT).first.click()
        try:
            await self.page.wait_for_url(lambda u: not self.on_login_page(u), timeout=25000)
        except PWTimeout:
            if "/checkpoint/" in self.page.url:
                return False, "LinkedIn asked for a verification code"
            return False, "login did not complete (wrong details or a check)"
        await self.pause()
        return True, "logged in"

    # ------------------------------------------------------------ apply

    async def apply(self, url: str) -> tuple[JobStatus, str]:
        await self._goto(url)
        await self.page.wait_for_timeout(3000)
        if self.on_login_page(self.page.url):
            return JobStatus.NEEDS_MANUAL, "not logged in to LinkedIn - use 'Log in to LinkedIn' in the web UI"
        if await self._visible(ALREADY_APPLIED):
            return JobStatus.APPLIED, "already applied"
        if not await self._visible(EASY_APPLY, 4000):
            if await self._visible(GUEST_SIGN_IN):  # the public job page: no apply buttons for visitors
                return JobStatus.NEEDS_MANUAL, "not logged in to LinkedIn - use 'Log in to LinkedIn' in the web UI"
            if await self._visible(OTHER_APPLY):
                return JobStatus.NEEDS_MANUAL, "applies on company site"
            return JobStatus.NEEDS_MANUAL, "Easy Apply button not found"
        await self.page.locator(EASY_APPLY).first.click()
        return await self._easy_apply()

    async def _easy_apply(self) -> tuple[JobStatus, str]:
        """Walk the Easy Apply steps. Answers come only from the profile; anything else stops for the user."""
        answered: list[str] = []
        previous = None
        for _ in range(MAX_APPLY_STEPS):
            await self.page.wait_for_timeout(2500)
            if await visible(self.page, SUBMITTED):
                if await visible(self.page, DISMISS):
                    await self.page.locator(DISMISS).first.click()
                await self.pause()
                extra = f" (answered: {'; '.join(answered)})" if answered else ""
                return JobStatus.APPLIED, "applied on LinkedIn (Easy Apply)" + extra
            modal = self.page.locator(MODAL).first
            if not await visible(self.page, MODAL):
                return JobStatus.NEEDS_MANUAL, "the Easy Apply window closed unexpectedly - check the browser"
            if reason := await answer_form(modal, QUESTION_ITEM, answered, self.label):
                return JobStatus.NEEDS_MANUAL, f"screening questions - {reason}; finish in the browser"
            await self._unfollow_company(modal)
            step = (await text_of(modal, "h2, h3"), await _progress(modal))
            if step == previous:  # the last click did not move on: a field we could not fill, or an error
                error = await text_of(modal, STEP_ERROR)
                return JobStatus.NEEDS_MANUAL, f"Easy Apply did not accept the step ({error or step[0]}) - finish it"
            previous = step
            if not await visible(modal, NEXT):
                return JobStatus.NEEDS_MANUAL, f"no Next/Submit button on '{step[0]}' - finish in the browser"
            await modal.locator(NEXT).first.click()
        return JobStatus.NEEDS_MANUAL, "too many steps in Easy Apply - finish in the browser"

    async def _unfollow_company(self, modal) -> None:
        """The review step ticks "Follow <company>" by default; applying should not also follow the company."""
        box = modal.locator(FOLLOW_COMPANY).first
        try:
            if await box.count() and await box.is_checked():
                await modal.locator(f"label[for='{await box.get_attribute('id')}']").first.click()
        except PWTimeout:
            pass


async def _progress(modal) -> str:
    try:
        return await modal.locator("[role='progressbar']").first.get_attribute("aria-valuenow", timeout=1000) or ""
    except PWTimeout:
        return ""
