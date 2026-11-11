"""
Nifty option expiry calendar.

NIFTY option expiry schedule, verified against the NSE F&O archive
(tests/test_expiry_calendar_vs_archive.py):
  Weekly and monthly expiries fall on Thursday up to and including
  2025-08-28 (the first weekly expiry in the archive is 2019-02-14).
  Weekly and monthly expiries fall on Tuesday from 2025-09-02.
  Monthly expiry = the last weekly expiry of the month, so the monthly
  weekday switches in the same week as the weekly one.
  There was no Monday-expiry era for NIFTY.

Holidays: when a raw expiry day is a segment-closed exchange holiday, the
expiry moves to the previous trading day. Two sources are merged:
  - data/nse_holidays.json ``holidays``: forward-looking NSE holiday list;
  - data/nse_holidays.json ``historical_expiry_closures``: raw expiry days
    before 2026-10-19 that the archive shows as shifted (19 days, 2019-2026).
Muhurat sessions do not shift expiries by themselves.

``ref_date`` arguments: the expiry weekday is always chosen from the date of
the candidate expiry itself (not from the query date), so a query made on
2025-08-29 for September 2025 correctly sees Tuesday expiries. ``ref_date``
on ``get_monthly_expiry`` is accepted for backward compatibility and no longer
affects the result.
"""

import calendar
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# NSE expiry weekday cutover: Thursday before, Tuesday from this date. The archive
# shows the last Thursday expiry on 2025-08-28 and the first Tuesday expiry on
# 2025-09-02; any value in 2025-08-29 .. 2025-09-02 yields the same schedule.
_TUESDAY_CUTOVER = date(2025, 9, 1)

# First weekly NIFTY expiry in the archive. Earlier dates keep the Thursday
# convention (no archive coverage to verify against).
_FIRST_OBSERVED_WEEKLY = date(2019, 2, 14)

# Holiday shifts can chain over several closed days; bound the lookahead used
# when scanning raw expiry days so a shifted expiry is never missed.
_MAX_SHIFT_DAYS = 14

_HOLIDAYS_PATH = Path(__file__).resolve().parent / "nse_holidays.json"
_holiday_cache: Optional[dict[date, dict]] = None
_historical_cache: Optional[dict[date, dict]] = None


# ── Holiday calendar ────────────────────────────────────────────────────────

def _load_holidays() -> dict[date, dict]:
    """Load the forward-looking ``holidays`` list of data/nse_holidays.json once, keyed by date."""
    global _holiday_cache
    if _holiday_cache is None:
        payload = json.loads(_HOLIDAYS_PATH.read_text(encoding="utf-8"))
        _holiday_cache = {
            date.fromisoformat(entry["date"]): entry
            for entry in payload.get("holidays", [])
        }
    return _holiday_cache


def _load_historical_closures() -> dict[date, dict]:
    """Archive-derived raw expiry days that were exchange holidays (``historical_expiry_closures``)."""
    global _historical_cache
    if _historical_cache is None:
        payload = json.loads(_HOLIDAYS_PATH.read_text(encoding="utf-8"))
        _historical_cache = {
            date.fromisoformat(entry["date"]): entry
            for entry in payload.get("historical_expiry_closures", [])
        }
    return _historical_cache


def _closed_holiday_dates() -> frozenset[date]:
    """Dates on which the equity and derivatives segments are closed (forward + historical)."""
    forward = (d for d, h in _load_holidays().items() if h.get("segment_closed"))
    historical = (d for d, h in _load_historical_closures().items() if h.get("segment_closed"))
    return frozenset(forward) | frozenset(historical)


def get_holiday(d: date) -> Optional[dict]:
    """Holiday record for ``d`` (any kind, including Muhurat), or None.

    Historical expiry closures are returned for dates before the forward list.
    """
    return _load_holidays().get(d) or _load_historical_closures().get(d)


def is_trading_day(d: date) -> bool:
    """True when ``d`` is a weekday and not a segment-closed NSE holiday."""
    return d.weekday() < 5 and d not in _closed_holiday_dates()


def is_market_open_day(d: date) -> bool:
    """Alias of :func:`is_trading_day` for call sites that gate live modes."""
    return is_trading_day(d)


def previous_trading_day(d: date) -> date:
    """Latest trading day strictly before ``d``."""
    cur = d - timedelta(days=1)
    for _ in range(_MAX_SHIFT_DAYS * 4):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    raise RuntimeError(f"No trading day found within {_MAX_SHIFT_DAYS * 4} days before {d}")


def _expiry_shift(d: date) -> date:
    """Expiry actually traded for raw expiry day ``d`` (previous trading day if closed)."""
    return d if is_trading_day(d) else previous_trading_day(d)


# ── Backward-compat; callers in legacy code path ───────────────────────────

def _expiry_weekday(expiry_date: date) -> int:
    """Calendar weekday of the raw NIFTY weekly/monthly expiry for a contract expiring on ``expiry_date``."""
    if expiry_date >= _TUESDAY_CUTOVER:
        return calendar.TUESDAY
    return calendar.THURSDAY


def _all_weekday_in_month(year: int, month: int, weekday: int) -> list[date]:
    """All dates in (year, month) that fall on ``weekday`` (calendar.TUESDAY etc.)."""
    cal = calendar.monthcalendar(year, month)
    return [
        date(year, month, week[weekday])
        for week in cal
        if week[weekday] != 0
    ]


def get_all_thursdays(year: int, month: int) -> list[date]:
    """Get all Thursdays in a given month (legacy helper; Thursday-era only)."""
    return _all_weekday_in_month(year, month, calendar.THURSDAY)


# ── Core helpers ────────────────────────────────────────────────────────────

def is_thursday_expiry_era(ref_date: date) -> bool:
    """True while NIFTY weekly expiries fall on Thursday (before the 2025-09-01 Tuesday cutover).

    The Thursday era runs from the first observed weekly (2019-02-14) through
    2025-08-28; earlier dates are included by convention.
    """
    return ref_date < _TUESDAY_CUTOVER


def get_monthly_expiry(year: int, month: int, ref_date: Optional[date] = None) -> date:
    """
    Last expiry day of the month: the last Thursday (pre-cutover) or Tuesday
    (post-cutover), shifted to the previous trading day if that day is an NSE
    holiday. The weekday is chosen from the month's own expiry date, so
    ``ref_date`` is accepted for backward compatibility but is not used.
    """
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    weekday = _expiry_weekday(last_day)
    days = _all_weekday_in_month(year, month, weekday)
    return _expiry_shift(max(days))


def get_weekly_expiries(start_date: date, end_date: date) -> list[date]:
    """All weekly expiry dates (Tuesdays or Thursdays by era) in [start, end]."""
    expiries: set[date] = set()
    current = start_date
    scan_end = end_date + timedelta(days=_MAX_SHIFT_DAYS)
    while current <= scan_end:
        if current.weekday() == _expiry_weekday(current):
            shifted = _expiry_shift(current)
            if start_date <= shifted <= end_date:
                expiries.add(shifted)
        current += timedelta(days=1)
    return sorted(expiries)


def get_monthly_expiries(start_date: date, end_date: date) -> list[date]:
    """All monthly expiry dates in [start, end]."""
    expiries: list[date] = []
    y, m = start_date.year, start_date.month
    while True:
        exp = get_monthly_expiry(y, m)
        if exp > end_date:
            break
        if exp >= start_date:
            expiries.append(exp)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return expiries


def get_next_monthly_expiry(from_date: date) -> date:
    """Next monthly expiry on or after ``from_date``."""
    y, m = from_date.year, from_date.month
    for _ in range(3):
        exp = get_monthly_expiry(y, m)
        if exp >= from_date:
            return exp
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return get_monthly_expiry(y, m)


def get_best_expiry_for_dte(from_date: date, target_dte: int) -> tuple[date, int]:
    """Monthly expiry closest to ``target_dte`` days out. Returns (expiry, actual_dte)."""
    candidates: list[tuple[date, int, int]] = []
    y, m = from_date.year, from_date.month
    for _ in range(4):
        exp = get_monthly_expiry(y, m)
        if exp > from_date:
            actual_dte = (exp - from_date).days
            candidates.append((exp, actual_dte, abs(actual_dte - target_dte)))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    if not candidates:
        exp = get_next_monthly_expiry(from_date)
        return exp, (exp - from_date).days
    best = min(candidates, key=lambda x: x[2])
    return best[0], best[1]


def format_expiry_label(expiry: date) -> str:
    """Human-readable expiry label, e.g. '28 Apr 2026'."""
    return f"{expiry.day} {expiry.strftime('%b %Y')}"  # portable (no %-d on Windows)


# ── Weekly helpers ──────────────────────────────────────────────────────────

def get_next_weekly_expiry(from_date: date) -> tuple[date, int]:
    """
    Next weekly expiry *after* ``from_date`` (Tuesday or Thursday by the era of
    the candidate expiry), holiday-shifted. Returns (expiry_date, dte) with DTE
    measured to the shifted expiry.
    """
    current = from_date + timedelta(days=1)
    while True:
        if current.weekday() == _expiry_weekday(current):
            shifted = _expiry_shift(current)
            if shifted > from_date:
                return shifted, (shifted - from_date).days
        current += timedelta(days=1)


def get_weekly_expiry_in_range(
    from_date: date,
    min_dte: int,
    max_dte: int,
    lookahead_cycles: int = 3,
) -> tuple[Optional[date], int]:
    """
    Return the first weekly expiry whose DTE falls inside ``[min_dte, max_dte]``.

    This is more robust than taking the immediate next expiry because after an
    exchange weekday shift the nearest expiry may be too close (for example,
    Monday entry with Tuesday expiry). In that case we intentionally skip to the
    following weekly cycle if it lands inside the desired DTE window.

    DTE is measured to the holiday-shifted expiry (e.g. a Tuesday expiry moved
    to Monday by a holiday is one day closer).
    """
    if min_dte > max_dte:
        return None, 0

    anchor = from_date
    current = from_date
    for _ in range(max(1, lookahead_cycles)):
        expiry, _ = get_next_weekly_expiry(current)
        dte = (expiry - anchor).days
        if min_dte <= dte <= max_dte:
            return expiry, dte
        current = expiry
    return None, 0


def get_upcoming_weekly_expiries(from_date: date, count: int = 4) -> list[dict]:
    """Next ``count`` weekly expiries (Tuesday or Thursday by era) with DTE info."""
    results: list[dict] = []
    seen: set[date] = set()
    current = from_date + timedelta(days=1)
    while len(results) < count:
        if current.weekday() == _expiry_weekday(current):
            exp = _expiry_shift(current)
            if exp > from_date and exp not in seen:
                seen.add(exp)
                dte = (exp - from_date).days
                results.append({
                    "expiry": exp,
                    "label": format_expiry_label(exp),
                    "dte": dte,
                    "is_monthly": exp == get_monthly_expiry(exp.year, exp.month),
                })
        current += timedelta(days=1)
    return results


# ── Monthly-only listing (used by existing backtest / signal paths) ─────────

def get_upcoming_expiries(from_date: date, count: int = 5) -> list[dict]:
    """
    Next ``count`` *monthly* expiries with DTE info.
    Weekday is Tuesday or Thursday depending on each expiry's date.
    """
    results: list[dict] = []
    y, m = from_date.year, from_date.month
    for _ in range(24):
        exp = get_monthly_expiry(y, m)
        if exp > from_date:
            dte = (exp - from_date).days
            results.append({
                "expiry": exp,
                "label": format_expiry_label(exp),
                "dte": dte,
                "is_monthly": True,
                "is_weekly": False,
                "is_current_month": exp.month == from_date.month,
            })
            if len(results) >= count:
                break
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return results


# ── Combined weekly+monthly listing (used by signal-combined & ExpirySelector) ─

def get_all_expiries(from_date: date, count: int = 8) -> list[dict]:
    """
    Next ``count`` expiries mixing both weekly AND monthly dates.

    Every weekly expiry day (Thursday or Tuesday by era) is a potential expiry;
    the last such day of each month is additionally tagged as monthly.
    Holiday-shifted expiries are reported on the trading day they actually settle.

    Useful for ExpirySelector (backtest + signal) so it can compare
    short-DTE weekly trades against longer-DTE monthly ones.
    """
    results: list[dict] = []
    seen: set[date] = set()
    current = from_date + timedelta(days=1)
    while len(results) < count:
        if current.weekday() == _expiry_weekday(current):
            exp = _expiry_shift(current)
            if exp > from_date and exp not in seen:
                seen.add(exp)
                dte = (exp - from_date).days
                is_monthly = exp == get_monthly_expiry(exp.year, exp.month, ref_date=exp)
                results.append({
                    "expiry": exp,
                    "label": format_expiry_label(exp),
                    "dte": dte,
                    "is_monthly": is_monthly,
                    "is_weekly": not is_monthly,
                })
        current += timedelta(days=1)
    return results
