"""Read an annual package in lakhs out of a portal's salary text."""

from __future__ import annotations

import re

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
MONTHLY_RE = re.compile(r"per month|a month|/\s*month|monthly|\bp\.?\s?m\.?\b", re.I)
LAKH_RE = re.compile(r"\blacs?\b|\blakhs?\b|\blpa\b", re.I)
# Without one of these, a bare number ("2 openings") is not a salary and must not be read as one.
SALARY_HINT_RE = re.compile(r"₹|\brs\.?\b|\binr\b|\blacs?\b|\blakhs?\b|\blpa\b|\bctc\b|per annum|p\.?\s?a\.?\b"
                            r"|a year|per year|per month|a month|monthly", re.I)
UP_TO_RE = re.compile(r"\bup\s?to\b", re.I)
RUPEES_FROM = 10000  # at or above this a number is rupees; below it the listing is already talking in lakhs


def _to_lpa(n: float, monthly: bool) -> float:
    """One number from a salary string as an annual package in lakhs."""
    if monthly:
        n *= 12
    return n / 100000 if n >= RUPEES_FROM else n  # "3-6 Lacs" and "1.5 lakh per month" are already in lakhs


def parse_lpa(text: str) -> tuple[float | None, float | None]:
    """(min, max) annual package in lakhs from a portal's salary string; (None, None) when it states no figure."""
    if not text or not SALARY_HINT_RE.search(text):
        return None, None
    monthly = bool(MONTHLY_RE.search(text))
    values = [_to_lpa(float(m.group().replace(",", "")), monthly) for m in NUM_RE.finditer(text)]
    values = [v for v in values if v]
    if not values:
        return None, None
    if UP_TO_RE.search(text):
        return None, max(values)  # "Up to 10 Lacs" states a ceiling, not a range
    return min(values), max(values)


def meets_salary_floor(text: str, min_lpa: float | None) -> bool:
    """False only when the posting states a package and the top of its range is below your floor.

    An unstated salary always passes. "Not disclosed" is the norm on Indian job boards, so filtering on an
    unknown would cut far more good jobs than noise.
    """
    if not min_lpa:
        return True
    lo, hi = parse_lpa(text)
    top = hi if hi is not None else lo
    return top is None or top >= min_lpa
