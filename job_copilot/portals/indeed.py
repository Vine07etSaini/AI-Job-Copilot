"""Indeed via the shared debug Chrome (same browser, profile and live view as Naukri).

Search and job pages work without an account; applying needs a signed-in session. Indeed signs in with an emailed
code, so log in once through the web UI ("Log in to Indeed" + the live view) or `python main.py login indeed`.
Only "Easily apply" (Indeed Apply) jobs are applied to; "Apply on company site" jobs go to you as NEEDS_MANUAL.
Indeed changes its markup often, so selectors are kept in one place with fallbacks.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urlparse

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PWTimeout

from job_copilot.agents.freshness import parse_posted
from job_copilot.models import JobPosting, JobStatus
from job_copilot.portals import live_view
from job_copilot.portals.browser import ChromePortal
from job_copilot.portals.forms import answer_form, text_of, visible

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://in.indeed.com"
LOGIN_URL = "https://secure.indeed.com/account/login"
LOGIN_HOST = "secure.indeed.com"
SIGN_IN_LINK = "a[data-gnav-element-name='SignIn'], a[href*='secure.indeed.com/account/login']"
ACCOUNT_MENU = "[data-gnav-element-name='AccountMenu'], #AccountMenu, button[aria-label*='account' i]"
RESULTS = "a[data-jk], .job_seen_beacon"
DESCRIPTION = "[class*='simple-job-description'], #jobDescriptionText"
ALREADY_APPLIED = "text=/you applied|applied on .{0,20}\\d|application submitted/i"
COMPANY_SITE = ("[data-testid='viewjob-apply']:has-text('company site'), #applyButtonLinkContainer a, "
                "a:has-text('Apply on company site'), button:has-text('Apply on company site')")
APPLY_BUTTON = ("[data-testid='viewjob-indeed-apply'], #indeedApplyButton, button[data-testid*='indeedApplyButton'], "
                ".ia-IndeedApplyButton")
# Indeed Apply ("smartapply") steps: contact info -> resume -> employer questions -> review -> submit.
QUESTION_ITEM = ".ia-Questions-item, [class*='Questions-item'], fieldset"
CONTINUE = ("button[data-testid*='continue-button'], button:has-text('Continue'), "
            "button:has-text('Review your application')")
SUBMIT = "button:has-text('Submit your application'), button:has-text('Submit application')"
SUBMITTED = "text=/application has been submitted|your application was sent|you've applied|you have applied/i"
MAX_APPLY_STEPS = 12
POSTED_RE = re.compile(r"just posted|today|\d+\+?\s*(?:day|hour)s?\s*ago", re.I)

CARDS_JS = """
() => Array.from(document.querySelectorAll('a[data-jk]')).map(a => {
  const card = a.closest('.job_seen_beacon') || a.closest('li') || a.parentElement;
  const txt = s => (card.querySelector(s)?.innerText || '').trim();
  return {
    id: a.getAttribute('data-jk') || '',
    title: (a.querySelector('span[title]')?.getAttribute('title') || a.innerText || '').trim(),
    company: txt('[data-testid="company-name"], .companyName'),
    location: txt('[data-testid="text-location"], .companyLocation'),
    salary: txt('[data-testid="attribute_snippet_testid"], .salary-snippet-container'),
    posted: txt('[data-testid="myJobsStateDate"], span.date'),
    snippet: txt('[data-testid="jobsnippet_footer"], .job-snippet, [class*="jobSnippet"]'),
    easyApply: !!card.querySelector('[data-testid="indeedApply"], .iaLabel, .ialbl') || /easily apply/i.test(card.innerText),
  };
})
"""

DETAILS_JS = """
() => {
  const txt = s => (document.querySelector(s)?.innerText || '').trim();
  return {
    description: txt('[class*="simple-job-description"]') || txt('#jobDescriptionText')
      || txt('[data-testid="viewjob-job-content"]'),
    posted: txt('[data-testid="jobsearch-JobMetadataFooter"], .jobsearch-JobMetadataFooter'),
    company: txt('[data-testid="inlineHeader-companyName"], [data-company-name="true"]'),
  };
}
"""



def search_url(base: str, role: str, location: str, start: int, max_age_days: int) -> str:
    """`fromage` limits results to jobs posted within that many days; `start` pages in steps of 10."""
    params = {"q": role, "l": location or "", "fromage": max_age_days}
    if start:
        params["start"] = start
    return f"{base}/jobs?{urlencode(params)}"


def clean_posted(text: str) -> str:
    """The date part of Indeed's "Posted 3 days ago" / "Just posted" texts.

    "Employer active 2 days ago" is when the employer last logged in, not when the job was posted, so it is ignored.
    """
    if re.search(r"\bactive\b", text or "", re.I):
        return ""
    m = POSTED_RE.search(text or "")
    return m.group(0) if m else ""


class IndeedPortal(ChromePortal):
    name = "indeed"
    label = "Indeed"
    login_url = LOGIN_URL

    @property
    def base(self) -> str:
        return (self.settings.base_url or DEFAULT_BASE).rstrip("/")

    # ------------------------------------------------------------ search

    async def search(self, role: str, location: str, max_age_days: int, experience_years: float,
                     target_cards: int = 0) -> list[JobPosting]:
        results: list[JobPosting] = []
        seen: set[str] = set()
        pages_done, new_on_page = 0, 0
        while pages_done == 0 or self.keep_paging(pages_done, new_on_page, len(results), target_cards):
            if pages_done:
                await self.pause()
            url = search_url(self.base, role, location, pages_done * 10, max_age_days)
            pages_done += 1
            log.info("Indeed search: %s", url)
            await self._goto(url)
            try:
                await self.page.wait_for_selector(RESULTS, timeout=15000)
            except PWTimeout:
                log.info("No job cards on %s", url)
                break
            cards = [c for c in await self.page.evaluate(CARDS_JS) if c["id"] and c["title"] and c["id"] not in seen]
            new_on_page = len(cards)
            for c in cards:
                seen.add(c["id"])
                posted = clean_posted(c["posted"])
                results.append(JobPosting(
                    portal=self.name, external_id=c["id"], title=c["title"], company=c["company"] or "Unknown",
                    url=f"{self.base}/viewjob?jk={c['id']}", location=c["location"], salary_text=c["salary"],
                    snippet=c["snippet"], posted_text=posted, posted_at=parse_posted(posted),
                ))
            log.info("Indeed page %d: %d new cards (%d collected)", pages_done, new_on_page, len(results))
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
        return urlparse(url).hostname == LOGIN_HOST

    async def is_logged_in(self) -> bool:
        """Signed-in visitors get an account menu instead of the "Sign in" link."""
        await self._goto(self.base)
        await self.page.wait_for_load_state("load")
        return not await self._visible(SIGN_IN_LINK) and await self._visible(ACCOUNT_MENU)

    # ------------------------------------------------------------ apply

    async def apply(self, url: str) -> tuple[JobStatus, str]:
        await self._goto(url)
        await self.page.wait_for_timeout(2500)
        if await self._visible(ALREADY_APPLIED):
            return JobStatus.APPLIED, "already applied"
        if await self._visible(COMPANY_SITE):
            return JobStatus.NEEDS_MANUAL, "applies on company site"
        button = self.page.locator(APPLY_BUTTON).first
        if not await self._visible(APPLY_BUTTON):
            return JobStatus.NEEDS_MANUAL, "Indeed Apply button not found"
        form = await self._open_apply_form(button)
        if form is not self.page:
            live_view.set_page(self.name, form)  # Indeed Apply opened its own tab: show that one instead
        try:
            return await self._indeed_apply(form)
        finally:
            if form is not self.page:
                live_view.set_page(self.name, self.page)
                await form.close()

    async def _open_apply_form(self, button: Locator) -> Page:
        """Indeed Apply opens either in this tab or in a new one; return the page that holds the form."""
        try:
            async with self.ctx.expect_page(timeout=8000) as new_page:
                await button.click()
            form = await new_page.value
            await form.wait_for_load_state("domcontentloaded")
            return form
        except PWTimeout:
            return self.page

    async def _indeed_apply(self, page: Page) -> tuple[JobStatus, str]:
        """Walk the Indeed Apply steps. Answers come only from the profile; anything else stops for the user."""
        answered: list[str] = []
        previous = None
        for _ in range(MAX_APPLY_STEPS):
            await page.wait_for_timeout(2500)
            host = urlparse(page.url).hostname or ""
            if self.on_login_page(page.url):
                return JobStatus.NEEDS_MANUAL, "not logged in to Indeed - use 'Log in to Indeed' in the web UI"
            if not host.endswith("indeed.com"):
                return JobStatus.NEEDS_MANUAL, f"Indeed Apply left indeed.com ({host}) - finish in the browser"
            if await visible(page, SUBMITTED):
                await self.pause()
                extra = f" (answered: {'; '.join(answered)})" if answered else ""
                return JobStatus.APPLIED, "applied on Indeed" + extra
            if reason := await answer_form(page, QUESTION_ITEM, answered, self.label):
                return JobStatus.NEEDS_MANUAL, f"screening questions - {reason}; finish in the browser"
            step = (page.url, await text_of(page, "h1, h2"))
            if step == previous:  # the last click did not move on: a required field we could not fill, or an error
                return JobStatus.NEEDS_MANUAL, f"Indeed did not accept the step '{step[1]}' - finish in the browser"
            previous = step
            for selector in (SUBMIT, CONTINUE):
                if await visible(page, selector):
                    await page.locator(selector).first.click()
                    break
            else:
                return JobStatus.NEEDS_MANUAL, f"no Continue button on '{step[1]}' - finish in the browser"
        return JobStatus.NEEDS_MANUAL, "too many steps in Indeed Apply - finish in the browser"
