"""Shared base for portals driven in the user's debug Chrome (attached over CDP) and shown in the web UI's live view.

Every browser portal uses the same Chrome and profile (storage/chrome-profile), so one browser keeps the logins for
all sites. Portals can run in parallel: each opens its own Chrome window (a background tab would stop rendering and
freeze its live view), and Chrome is only closed when the last portal session ends.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from job_copilot.config import PortalCredentials
from job_copilot.portals import live_view
from job_copilot.portals.base import Portal, PortalBlocked
from job_copilot.portals.chrome_debug import OFF_SCREEN, ensure_chrome, hide_from_taskbar, keep_awake, wait_until_down

log = logging.getLogger(__name__)

# Page titles that mean the site is challenging or refusing us (Cloudflare, captcha, WAF pages).
BLOCKED_TITLES = ("access denied", "captcha", "just a moment", "security check", "verify you are human",
                  "additional verification required", "attention required")
WINDOW_SIZE = {"width": 1280, "height": 900}
CONNECT_TIMEOUT = 30  # seconds; a wedged Chrome otherwise hangs the run forever

# Open portal sessions in this process. Only touched on the event loop thread, so no lock is needed.
_sessions = 0


class ChromePortal(Portal):
    """A portal that attaches to a normal, visible Chrome over CDP and opens its own window there."""

    label: str = ""      # name shown to the user, e.g. "Naukri"
    login_url: str = ""

    async def open(self) -> None:
        """Attach to a normal Chrome over CDP (started on demand) and open this portal's own window."""
        global _sessions
        self._login_attempted = False
        self._counted = False
        cdp_url = self.settings.cdp_url
        try:
            # Blocking subprocess/HTTP polling, so keep it off the event loop.
            await asyncio.to_thread(ensure_chrome, cdp_url)
            self._pw = await async_playwright().start()
            try:  # Playwright's own timeout does not cover a Chrome that accepts the socket and never answers
                self.browser = await asyncio.wait_for(self._pw.chromium.connect_over_cdp(cdp_url), CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                raise RuntimeError(f"Chrome on {cdp_url} is not responding. Close the copilot Chrome window "
                                   "(or end chrome.exe for storage/chrome-profile) and try again.") from None
            _sessions += 1
            self._counted = True
            if _sessions == 1:
                keep_awake(True)  # same thread (the event loop) as close(), which the API requires
            if self.browser.contexts:
                self.ctx: BrowserContext = self.browser.contexts[0]  # the default context holds the saved logins
            else:  # unexpected Chrome state: a fresh context has no saved login; say so instead of an IndexError
                log.warning("Chrome exposed no default browser context; using a new one (saved logins will not apply)")
                self.ctx = await self.browser.new_context()
            self.page: Page = await self._new_window()
            hide_from_taskbar(cdp_url)  # new windows get their own taskbar button, so do it after opening ours
            live_view.set_page(self.name, self.page)
        except BaseException:
            await self.close()  # __aexit__ does not run when __aenter__ fails
            raise

    async def _new_window(self) -> Page:
        """A page in a new, off-screen window: each portal renders on its own, so parallel live views stay live."""
        cdp = await self.browser.new_browser_cdp_session()
        try:
            target = await cdp.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
            # Every connection sees every new page, and another portal may be opening its window at the same moment,
            # so pick ours by target id rather than taking the first new page.
            page = await self._page_for_target(target["targetId"])
            try:  # new windows open on screen; park them off-screen like the first one
                win = await cdp.send("Browser.getWindowForTarget", {"targetId": target["targetId"]})
                await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"],
                                                           "bounds": {**OFF_SCREEN, **WINDOW_SIZE}})
            except Exception as e:  # cosmetic: the window is just visible
                log.debug("Could not move the window off-screen: %s", e)
            return page
        finally:
            await cdp.detach()

    async def _page_for_target(self, target_id: str, timeout: float = 10) -> Page:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for page in self.ctx.pages:
                session = await self.ctx.new_cdp_session(page)
                try:
                    if (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"] == target_id:
                        return page
                finally:
                    await session.detach()
            await asyncio.sleep(0.1)
        raise RuntimeError("Chrome opened a window but its page never appeared")

    async def close(self) -> None:
        """Close our window; the last session to end also quits Chrome (unless `close_chrome_on_finish` is off)."""
        global _sessions
        live_view.set_page(self.name, None)
        if getattr(self, "page", None) and not self.page.is_closed():
            await self.page.close()
        last = False
        if getattr(self, "_counted", False):
            self._counted = False
            _sessions -= 1
            last = _sessions == 0
            if last:
                keep_awake(False)
        if last and self.settings.close_chrome_on_finish and getattr(self, "browser", None):
            if user_pages(self.ctx.pages):
                log.info("Leaving Chrome open: a page you opened yourself is still showing")
                last = False
        if last and self.settings.close_chrome_on_finish and getattr(self, "browser", None):
            try:  # clean shutdown through CDP, so the profile (login cookies) is flushed to disk
                await (await self.browser.new_browser_cdp_session()).send("Browser.close")
            except Exception as e:  # already gone or refused: nothing more to do
                log.debug("Could not close Chrome: %s", e)
            # Chrome keeps answering on its port for a moment while it quits; a run starting then would attach to it.
            await asyncio.to_thread(wait_until_down, self.settings.cdp_url)
        if getattr(self, "_pw", None):
            await self._pw.stop()  # disconnects from Chrome
            self._pw = None

    async def show(self, url: str) -> None:
        """Open a page for the user to look at (and take over) in this portal's live view."""
        await self._goto(url)

    async def _goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await self._check_blocked()

    async def _check_blocked(self) -> None:
        title = (await self.page.title() or "").lower()
        if any(t in title for t in BLOCKED_TITLES):
            raise PortalBlocked(f"{self.label} blocked the request ({title}). Lower the volume and retry later.")

    async def _visible(self, selector: str, timeout: float = 1500) -> bool:
        try:
            return await self.page.locator(selector).first.is_visible(timeout=timeout)
        except PWTimeout:
            return False

    # ------------------------------------------------------------ login

    @abstractmethod
    def on_login_page(self, url: str) -> bool:
        """True while `url` is still part of the site's sign-in flow."""

    @abstractmethod
    async def is_logged_in(self) -> bool:
        """Open a page that tells a signed-in user apart from a visitor."""

    async def login(self, creds: PortalCredentials | None = None) -> tuple[bool, str]:
        """Automatic sign-in; portals that support it override this. Default: the user signs in by hand."""
        return False, f"{self.label} has no automatic login - log in through the live view"


def user_pages(pages: list) -> list:
    """Pages someone is looking at: anything but the blank start tab. Chrome is never quit under them."""
    return [p for p in pages if not p.is_closed() and p.url not in ("about:blank", "")]


ON_SCREEN = {"left": 80, "top": 60, "width": 1280, "height": 900, "windowState": "normal"}


async def open_in_chrome(url: str, cdp_url: str) -> None:
    """Open `url` in a normal, on-screen window of the copilot's Chrome (your saved logins), for you to use directly.

    Unlike a portal session this window is yours: it is not in the live view, the copilot never drives it, and
    Chrome stays open while it shows a page.
    """
    await asyncio.to_thread(ensure_chrome, cdp_url)
    pw = await async_playwright().start()
    try:
        browser = await asyncio.wait_for(pw.chromium.connect_over_cdp(cdp_url), CONNECT_TIMEOUT)
        cdp = await browser.new_browser_cdp_session()
        target = await cdp.send("Target.createTarget", {"url": url, "newWindow": True})
        win = await cdp.send("Browser.getWindowForTarget", {"targetId": target["targetId"]})
        await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": ON_SCREEN})
        await cdp.send("Target.activateTarget", {"targetId": target["targetId"]})  # bring it to the front
        await cdp.detach()
    finally:
        await pw.stop()  # only disconnects; the window stays open


async def interactive_login(portal: ChromePortal, creds: PortalCredentials | None, finished: asyncio.Event, on_state,
                            timeout: int = 300) -> bool:
    """Open the login page for the user. Try `creds` once if given, else wait while the user signs in by hand
    (through the web UI's live view). `on_state(state, message)` reports progress; `finished` ends the wait early."""
    async with portal:
        try:
            await portal._goto(portal.login_url)
            if not portal.on_login_page(portal.page.url):
                on_state("logged_in", "Already logged in.")
                return True
            if creds is not None:
                on_state("running", "Signing in with the details you entered...")
                ok, reason = await portal.login(creds)
                if ok:
                    on_state("logged_in", "Logged in.")
                    return True
                on_state("waiting", f"{reason}. Finish logging in using the live view (tick 'Take control').")
                if not portal.on_login_page(portal.page.url):
                    await portal._goto(portal.login_url)
            else:
                on_state("waiting", "Log in using the live view (tick 'Take control'), then press Done.")
            deadline = asyncio.get_running_loop().time() + timeout
            while portal.on_login_page(portal.page.url) and not finished.is_set():
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


async def login_session(portal: ChromePortal) -> str:
    """CLI login: sign in automatically if the portal can, else let the user sign in by hand in the opened window."""
    async with portal:
        try:
            ok, reason = await portal.login()
        except PortalBlocked as e:
            ok, reason = False, str(e)
        if ok:
            return f"automatic login: {reason}"
        log.warning("Automatic login not possible: %s", reason)
        if not portal.on_login_page(portal.page.url):
            await portal._goto(portal.login_url)
        prompt = f"Log in to {portal.label} in the opened browser window, then press Enter here... "
        await asyncio.to_thread(input, prompt)
        return "manual login"
