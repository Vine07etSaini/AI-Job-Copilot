"""Registry of the browser page a portal is driving, so the web UI can show a live view.

The real window stays open; this only lets the API stream and control the same page.
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
