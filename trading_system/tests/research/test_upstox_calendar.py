"""Tests for research.data.upstox.calendar."""

from __future__ import annotations

from datetime import date

from research.data.upstox import calendar as upstox_calendar
from core.market_hours import NSE_HOLIDAYS_2026


class TestIsTradingDay:
    def test_weekday_is_a_trading_day_when_not_a_holiday(self) -> None:
        # 2026-01-06 is a Tuesday and not in NSE_HOLIDAYS_2026.
        d = date(2026, 1, 6)
        assert d.weekday() < 5
        assert d not in NSE_HOLIDAYS_2026
        assert upstox_calendar.is_trading_day(d) is True

    def test_saturday_is_not_a_trading_day(self) -> None:
        d = date(2026, 1, 3)  # Saturday
        assert d.weekday() == 5
        assert upstox_calendar.is_trading_day(d) is False

    def test_sunday_is_not_a_trading_day(self) -> None:
        d = date(2026, 1, 4)  # Sunday
        assert d.weekday() == 6
        assert upstox_calendar.is_trading_day(d) is False

    def test_known_2026_holidays_are_excluded(self) -> None:
        for holiday in NSE_HOLIDAYS_2026:
            assert upstox_calendar.is_trading_day(holiday) is False

    def test_known_nse_holidays_alias_matches_core(self) -> None:
        assert upstox_calendar.KNOWN_NSE_HOLIDAYS == NSE_HOLIDAYS_2026


class TestExpectedBarCount:
    def test_one_minute_bars(self) -> None:
        assert upstox_calendar.expected_bar_count(date(2026, 1, 5), "minutes", 1) == 375

    def test_five_minute_bars(self) -> None:
        assert upstox_calendar.expected_bar_count(date(2026, 1, 5), "minutes", 5) == 75

    def test_fifteen_minute_bars(self) -> None:
        assert upstox_calendar.expected_bar_count(date(2026, 1, 5), "minutes", 15) == 25

    def test_day_interval_returns_none(self) -> None:
        assert upstox_calendar.expected_bar_count(date(2026, 1, 5), "days", 1) is None

    def test_non_dividing_interval_returns_none(self) -> None:
        # 375 minutes is not evenly divisible by 7.
        assert upstox_calendar.expected_bar_count(date(2026, 1, 5), "minutes", 7) is None

    def test_zero_interval_value_returns_none(self) -> None:
        assert upstox_calendar.expected_bar_count(date(2026, 1, 5), "minutes", 0) is None

    def test_negative_interval_value_returns_none(self) -> None:
        assert upstox_calendar.expected_bar_count(date(2026, 1, 5), "minutes", -5) is None

    def test_custom_session_bounds(self) -> None:
        from datetime import time

        count = upstox_calendar.expected_bar_count(
            date(2026, 1, 5),
            "minutes",
            30,
            session_start_ist=time(9, 0),
            session_end_ist=time(17, 0),
        )
        assert count == 16
