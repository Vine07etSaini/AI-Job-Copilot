"""Registry of the browser page a portal is driving, so the web UI can show a live view.

The real window stays open (headed); this only lets the API take screenshots of the same page.
"""

from __future__ import annotations

from playwright.async_api import Page

_page: Page | None = None


def set_page(page: Page | None) -> None:
    global _page
    _page = page


def is_active() -> bool:
    return _page is not None and not _page.is_closed()


def current_page() -> Page | None:
    return _page if is_active() else None


async def snapshot() -> bytes | None:
    """JPEG of the current page, or None when no browser is open (or it closed mid-capture)."""
    page = _page
    if page is None or page.is_closed():
        return None
    try:
        return await page.screenshot(type="jpeg", quality=55, timeout=5000)
    except Exception:  # page navigating/closing: skip this frame
        return None
