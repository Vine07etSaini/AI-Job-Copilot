"""Which portals are busy in this API process: different portals may run at the same time, the same portal never twice.

Each portal drives its own Chrome window; two runs on one portal would fight over it (and over its live view).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from job_copilot.portals import live_view
from job_copilot.portals.base import BROWSER_PORTALS

_busy: set[str] = set()


def acquire(*portals: str) -> list[str]:
    """Mark browser portals busy, or raise 409 naming the ones already in use. Returns what was claimed."""
    wanted = [p for p in dict.fromkeys(portals) if p in BROWSER_PORTALS]
    if taken := [p for p in wanted if p in _busy or live_view.is_active(p)]:
        raise HTTPException(409, f"{', '.join(taken)} is busy (a search, apply, login or an open posting in its live "
                                 "view). Close or finish that first; other portals can still run.")
    _busy.update(wanted)
    return wanted


def release(portals: list[str]) -> None:
    _busy.difference_update(portals)


@contextmanager
def claim(*portals: str) -> Iterator[None]:
    claimed = acquire(*portals)
    try:
        yield
    finally:
        release(claimed)


def busy_portals() -> list[str]:
    return sorted(_busy)
