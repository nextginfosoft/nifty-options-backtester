"""
NSE holiday support, holiday-shifted expiries, IST clock and the trading-day gate.

Covers:
- data/nse_holidays.json ``holidays`` is forward-looking only
- Holiday-shifted weekly/monthly expiries for 2026-10-19 .. 2026-12-31
- Historical expiries (2019-01-01 .. 2026-08-29) equal the pure weekday logic
  plus the archive-derived ``historical_expiry_closures`` (see test_expiry_calendar_vs_archive.py)
- previous_trading_day across weekends and chained holidays
- market_day_gate / is_market_open_day for the signal entry gate
- now_ist() and is_market_hours() with a monkeypatched clock
"""

import json
import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import data.expiry_calendar as ec
from utils import market_clock
from utils.market_clock import is_market_hours, market_day_gate, now_ist

HOLIDAYS_FILE = Path(__file__).resolve().parent.parent / "data" / "nse_holidays.json"
FORWARD_CUTOFF = date(2026, 10, 19)
HIST_START = date(2019, 1, 1)
HIST_END = date(2026, 8, 29)


# ---------------------------------------------------------------------------
# Reference implementations: the pre-holiday (pure weekday) logic
# ---------------------------------------------------------------------------

def _ref_weekly(start, end):
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() == ec._expiry_weekday(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _ref_monthly(year, month):
    last = date(year, month, calendar.monthrange(year, month)[1])
    wd = ec._expiry_weekday(last)
    return max(d for d in (date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1))
               if d.weekday() == wd)


def _ref_next_weekly(from_date):
    cur = from_date + timedelta(days=1)
    while cur.weekday() != ec._expiry_weekday(cur):   # era of the candidate expiry
        cur += timedelta(days=1)
    return cur, (cur - from_date).days


def _ref_in_range(from_date, min_dte, max_dte, cycles=3):
    anchor = current = from_date
    for _ in range(max(1, cycles)):
        expiry, _ = _ref_next_weekly(current)
        dte = (expiry - anchor).days
        if min_dte <= dte <= max_dte:
            return expiry, dte
        current = expiry
    return None, 0


def _ref_all_expiries(from_date, count):
    out = []
    cur = from_date + timedelta(days=1)
    while len(out) < count:
        if cur.weekday() == ec._expiry_weekday(cur):
            is_m = cur == _ref_monthly(cur.year, cur.month)
            out.append((cur, (cur - from_date).days, is_m))
        cur += timedelta(days=1)
    return out


ARCHIVE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nifty_expiries_archive.json"


def _archive_closures() -> dict:
    """raw expiry day -> shifted expiry, from the archive fixture (historical holidays)."""
    payload = json.loads(ARCHIVE_FIXTURE.read_text(encoding="utf-8"))
    return {date.fromisoformat(c["raw"]): date.fromisoformat(c["expiry"])
            for c in payload["raw_day_closures"]}


def _days(start, end):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


@pytest.fixture
def no_holidays(monkeypatch):
    """Empty the closed-holiday set so the calendar reduces to pure weekday logic."""
    monkeypatch.setattr(ec, "_closed_holiday_dates", lambda: frozenset())


@pytest.fixture
def holidays_on(monkeypatch):
    """Force specific closed holidays for chained-shift tests."""
    def _set(dates):
        monkeypatch.setattr(ec, "_closed_holiday_dates", lambda: frozenset(dates))
    return _set


# ---------------------------------------------------------------------------
# Holiday data file
# ---------------------------------------------------------------------------

class TestHolidayFile:
    def _entries(self):
        payload = json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8"))
        return payload["holidays"]

    def test_file_is_forward_looking_only(self):
        """Any holiday before the cutoff would change historical expiries."""
        for entry in self._entries():
            assert date.fromisoformat(entry["date"]) >= FORWARD_CUTOFF, entry

    def test_required_fields_present(self):
        for entry in self._entries():
            for key in ("date", "name", "segment_closed", "verified", "source"):
                assert key in entry, (entry, key)

    def test_expected_forward_holidays_listed(self):
        closed = {e["date"]: e for e in self._entries() if e["segment_closed"]}
        assert set(closed) == {"2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"}   # Christmas added 2026-11-26

    def test_muhurat_session_is_not_a_closure(self):
        muhurat = {e["date"]: e for e in self._entries() if e.get("muhurat_session")}
        assert "2026-11-08" in muhurat
        assert muhurat["2026-11-08"]["segment_closed"] is False

    def test_verified_entries_cite_a_source(self):
        for entry in self._entries():
            if entry["verified"]:
                assert entry["source"].startswith("https://www.nseindia.com/"), entry


# ---------------------------------------------------------------------------
# Trading days and previous_trading_day
# ---------------------------------------------------------------------------

class TestTradingDays:
    def test_holidays_are_not_trading_days(self):
        assert ec.is_trading_day(date(2026, 10, 20)) is False
        assert ec.is_trading_day(date(2026, 11, 10)) is False
        assert ec.is_trading_day(date(2026, 11, 24)) is False

    def test_muhurat_sunday_is_not_a_trading_day_by_weekday_only(self):
        # Sunday is a weekend; the Muhurat record must not open the market.
        assert ec.is_trading_day(date(2026, 11, 8)) is False

    def test_regular_days_are_trading_days(self):
        assert ec.is_trading_day(date(2026, 10, 19)) is True
        assert ec.is_market_open_day(date(2026, 10, 19)) is True

    def test_previous_trading_day_skips_weekend(self):
        # Monday -> previous Friday
        assert ec.previous_trading_day(date(2026, 10, 19)) == date(2026, 10, 16)
        # Monday 11-09 -> Friday 11-06 (Sat/Sun skipped)
        assert ec.previous_trading_day(date(2026, 11, 9)) == date(2026, 11, 6)

    def test_previous_trading_day_skips_holiday(self):
        # Tuesday holiday 10-20 -> Monday 10-19
        assert ec.previous_trading_day(date(2026, 10, 20)) == date(2026, 10, 19)
        # Tuesday holiday 11-10 -> Monday 11-09
        assert ec.previous_trading_day(date(2026, 11, 10)) == date(2026, 11, 9)

    def test_previous_trading_day_chains_over_closed_days(self, holidays_on):
        holidays_on({date(2026, 10, 19), date(2026, 10, 20)})
        # Mon 10-19 and Tue 10-20 closed -> Fri 10-16
        assert ec.previous_trading_day(date(2026, 10, 20)) == date(2026, 10, 16)


# ---------------------------------------------------------------------------
# Holiday-shifted expiries for 2026-10-19 .. 2026-12-31
# ---------------------------------------------------------------------------

class TestShiftedExpiries:
    START = date(2026, 10, 19)
    END = date(2026, 12, 31)

    def test_weekly_expiries_before_and_after_shift(self, no_holidays):
        before = [d.isoformat() for d in ec.get_weekly_expiries(self.START, self.END)]
        assert before == [
            "2026-10-20", "2026-10-27", "2026-11-03", "2026-11-10", "2026-11-17",
            "2026-11-24", "2026-12-01", "2026-12-08", "2026-12-15", "2026-12-22", "2026-12-29",
        ]

    def test_weekly_expiries_after_shift(self):
        after = [d.isoformat() for d in ec.get_weekly_expiries(self.START, self.END)]
        assert after == [
            "2026-10-19",  # Tue 10-20 Dussehra -> Mon 10-19
            "2026-10-27",
            "2026-11-03",
            "2026-11-09",  # Tue 11-10 Diwali Balipratipada -> Mon 11-09
            "2026-11-17",
            "2026-11-23",  # Tue 11-24 Guru Nanak -> Mon 11-23 (also monthly)
            "2026-12-01",
            "2026-12-08",
            "2026-12-15",
            "2026-12-22",
            "2026-12-29",
        ]

    def test_monthly_expiries_before_and_after_shift(self, no_holidays):
        assert [d.isoformat() for d in ec.get_monthly_expiries(self.START, self.END)] == [
            "2026-10-27", "2026-11-24", "2026-12-29",
        ]

    def test_monthly_expiries_after_shift(self):
        assert [d.isoformat() for d in ec.get_monthly_expiries(self.START, self.END)] == [
            "2026-10-27", "2026-11-23", "2026-12-29",
        ]

    def test_get_monthly_expiry_november_shifted(self):
        assert ec.get_monthly_expiry(2026, 11) == date(2026, 11, 23)
        assert ec.get_monthly_expiry(2026, 10) == date(2026, 10, 27)

    def test_get_all_expiries_reports_shifted_monthly_once(self):
        rows = ec.get_all_expiries(date(2026, 10, 19), count=5)
        assert [r["expiry"].isoformat() for r in rows] == [
            "2026-10-27", "2026-11-03", "2026-11-09", "2026-11-17", "2026-11-23",
        ]
        assert rows[-1]["is_monthly"] is True
        assert rows[0]["is_monthly"] is True
        assert rows[1]["is_monthly"] is False
        assert len({r["expiry"] for r in rows}) == len(rows)

    def test_upcoming_weekly_expiries_dte_to_shifted_date(self):
        rows = ec.get_upcoming_weekly_expiries(date(2026, 10, 16), count=2)
        # Friday 10-16: next expiry is Mon 10-19 (shifted), DTE 3 not 4
        assert rows[0]["expiry"] == date(2026, 10, 19)
        assert rows[0]["dte"] == 3

    def test_get_next_weekly_expiry_skips_shifted_onto_today(self):
        # Monday 10-19: Tue 10-20 shifts back onto 10-19 (not after today),
        # so the next expiry is Tue 10-27.
        assert ec.get_next_weekly_expiry(date(2026, 10, 19)) == (date(2026, 10, 27), 8)

    @pytest.mark.parametrize(
        "from_date, min_dte, max_dte, expected",
        [
            (date(2026, 10, 16), 3, 8, (date(2026, 10, 19), 3)),  # shifted expiry, DTE to 10-19
            (date(2026, 10, 19), 3, 8, (date(2026, 10, 27), 8)),
            (date(2026, 11, 6), 3, 8, (date(2026, 11, 9), 3)),    # 11-10 holiday -> Mon 11-09
            (date(2026, 11, 20), 3, 8, (date(2026, 11, 23), 3)),  # 11-24 holiday -> Mon 11-23
        ],
    )
    def test_weekly_expiry_in_range_uses_shifted_dte(self, from_date, min_dte, max_dte, expected):
        assert ec.get_weekly_expiry_in_range(from_date, min_dte, max_dte) == expected


# ---------------------------------------------------------------------------
# Historical invariance: expiries before 2026-10-19 are byte-identical
# ---------------------------------------------------------------------------

class TestHistoricalInvariance:
    """Pure weekday logic with no holidays, plus the archive-derived closures as the only historical change."""

    def test_weekly_and_monthly_lists_match_pure_weekday_logic(self, no_holidays):
        weekly = ec.get_weekly_expiries(HIST_START, HIST_END)
        assert weekly == _ref_weekly(HIST_START, HIST_END)
        monthly = ec.get_monthly_expiries(HIST_START, HIST_END)
        ref_monthly = []
        y, m = HIST_START.year, HIST_START.month
        while date(y, m, 1) <= HIST_END:
            d = _ref_monthly(y, m)
            if HIST_START <= d <= HIST_END:
                ref_monthly.append(d)
            m, y = (m + 1, y) if m < 12 else (1, y + 1)
        assert monthly == ref_monthly

    def test_historical_holidays_only_move_archive_closures(self):
        """With the real holiday data, the weekly list differs from pure weekday logic only at the archive closures."""
        closures = _archive_closures()
        expected = []
        for raw in _ref_weekly(HIST_START, HIST_END):
            expected.append(closures.get(raw, raw))
        assert ec.get_weekly_expiries(HIST_START, HIST_END) == sorted(set(expected))
        assert all(raw not in ec.get_weekly_expiries(HIST_START, HIST_END) for raw in closures)

    def test_every_historical_day_matches_reference_next_and_in_range(self, no_holidays):
        for d in _days(HIST_START, HIST_END):
            assert ec.get_next_weekly_expiry(d) == _ref_next_weekly(d), d
            assert ec.get_weekly_expiry_in_range(d, 3, 8) == _ref_in_range(d, 3, 8), d

    def test_every_historical_day_all_expiries_matches_reference(self, no_holidays):
        for d in _days(HIST_START, HIST_END):
            got = [(r["expiry"], r["dte"], r["is_monthly"]) for r in ec.get_all_expiries(d, count=4)]
            assert got == _ref_all_expiries(d, 4), d


# ---------------------------------------------------------------------------
# Entry gate helper and IST clock
# ---------------------------------------------------------------------------

class TestMarketDayGate:
    def test_dussehra_closes_market(self):
        msg = market_day_gate(date(2026, 10, 20))
        assert msg == "market closed (holiday: Dussehra)"

    def test_trading_day_is_open(self):
        assert market_day_gate(date(2026, 10, 19)) is None

    def test_weekend_is_closed(self):
        assert market_day_gate(date(2026, 10, 18)) == "market closed (weekend)"


class _FakeDatetime(datetime):
    """datetime subclass whose now() returns a fixed UTC instant converted to tz."""
    FIXED_UTC = None

    @classmethod
    def now(cls, tz=None):
        base = cls.FIXED_UTC
        return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)


@pytest.fixture
def clock_at(monkeypatch):
    def _set(utc_dt):
        _FakeDatetime.FIXED_UTC = utc_dt
        monkeypatch.setattr(market_clock, "datetime", _FakeDatetime)
    return _set


class TestIstClock:
    def test_now_ist_converts_utc_instant(self, clock_at):
        clock_at(datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc))
        now = now_ist()
        assert now.hour == 11 and now.minute == 30
        assert now.utcoffset() == timedelta(hours=5, minutes=30)

    def test_market_hours_true_at_1130_ist(self, clock_at):
        clock_at(datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc))  # 11:30 IST Monday
        assert is_market_hours() is True

    def test_market_hours_false_before_open(self, clock_at):
        clock_at(datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc))  # 08:30 IST
        assert is_market_hours() is False

    def test_market_hours_false_after_close(self, clock_at):
        clock_at(datetime(2026, 10, 19, 10, 5, tzinfo=timezone.utc))  # 15:35 IST
        assert is_market_hours() is False

    def test_market_hours_false_on_holiday(self, clock_at):
        clock_at(datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc))  # Dussehra, 11:30 IST
        assert is_market_hours() is False

    def test_naive_datetime_is_read_as_ist(self):
        assert is_market_hours(datetime(2026, 10, 19, 11, 30)) is True
        assert is_market_hours(datetime(2026, 10, 19, 16, 0)) is False

    def test_aware_non_ist_datetime_is_converted(self):
        # 06:00 UTC == 11:30 IST
        assert is_market_hours(datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)) is True


class TestSignalCombinedHolidayGate:
    """run_signal_combined must stop on a closed day before fetching data or generating signals."""

    def test_holiday_stops_signal_before_data_fetch(self, monkeypatch, capsys):
        import main as main_module
        from config import BacktestConfig

        def _fail_fetch(*args, **kwargs):
            raise AssertionError("market data must not be fetched on a closed day")

        monkeypatch.setattr(main_module, "now_ist",
                            lambda: datetime(2026, 10, 20, 12, 0, tzinfo=market_clock.IST))
        monkeypatch.setattr(main_module, "MarketDataFetcher", _fail_fetch)

        result = main_module.run_signal_combined(BacktestConfig())

        assert result is None
        out = capsys.readouterr().out
        assert "market closed (holiday: Dussehra)" in out
        assert "No signal generated" in out
