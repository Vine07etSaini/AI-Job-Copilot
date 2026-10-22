"""Registry of the browser page each portal is driving, so the web UI can show one live view per portal.

The real windows stay open; this only lets the API stream and control the same pages. Portals run in parallel,
each in its own Chrome window, so pages are kept per portal name.
"""

from __future__ import annotations

from playwright.async_api import Page

_pages: dict[str, Page] = {}


def set_page(portal: str, page: Page | None) -> None:
    if page is None:
        _pages.pop(portal, None)
    else:
        _pages[portal] = page


def current_page(portal: str) -> Page | None:
    page = _pages.get(portal)
    return page if page is not None and not page.is_closed() else None


def is_active(portal: str | None = None) -> bool:
    """Is this portal's browser open (or, without a name, any portal's)?"""
    return current_page(portal) is not None if portal else bool(active_portals())


def active_portals() -> list[str]:
    return [name for name in list(_pages) if current_page(name) is not None]
