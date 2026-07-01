"""Tests for parse_datetime_flexible date-hardening (FS T1.5)."""

from __future__ import annotations

from datetime import datetime

from placement_mail_tracker.utils.time import _MAX_YEAR_DELTA, _MIN_YEAR, parse_datetime_flexible


class TestYearOnlyRejection:
    def test_bare_year_returns_none(self):
        assert parse_datetime_flexible("2026") is None

    def test_bare_year_with_whitespace_returns_none(self):
        assert parse_datetime_flexible("  2026  ") is None

    def test_month_year_is_accepted(self):
        # "June 2026" is not year-only; defaults to June 1.
        result = parse_datetime_flexible("June 2026")
        assert result is not None
        assert result.year == 2026
        assert result.month == 6


class TestRangeValidation:
    def test_date_before_min_year_returns_none(self):
        assert parse_datetime_flexible("1999-06-15") is None

    def test_date_at_min_year_boundary_is_accepted(self):
        result = parse_datetime_flexible(f"{_MIN_YEAR}-01-15")
        assert result is not None
        assert result.year == _MIN_YEAR

    def test_date_far_future_returns_none(self):
        far_year = datetime.now().year + _MAX_YEAR_DELTA + 1
        assert parse_datetime_flexible(f"{far_year}-06-15") is None

    def test_date_at_max_year_boundary_is_accepted(self):
        max_year = datetime.now().year + _MAX_YEAR_DELTA
        result = parse_datetime_flexible(f"{max_year}-06-15")
        assert result is not None
        assert result.year == max_year


class TestNormalBehaviourPreserved:
    def test_iso_date_is_accepted(self):
        result = parse_datetime_flexible("2026-06-15")
        assert result is not None
        assert result.year == 2026
        assert result.month == 6
        assert result.day == 15

    def test_human_readable_full_month_name(self):
        result = parse_datetime_flexible("17 June 2026")
        assert result is not None
        assert result.year == 2026
        assert result.month == 6
        assert result.day == 17

    def test_dmy_with_abbreviated_month_and_time(self):
        result = parse_datetime_flexible("15-Jun-2026 05:30 PM")
        assert result is not None
        assert result.month == 6
        assert result.day == 15

    def test_month_day_year_comma_format(self):
        result = parse_datetime_flexible("June 17, 2026")
        assert result is not None
        assert result.year == 2026
        assert result.month == 6

    def test_none_input_returns_none(self):
        assert parse_datetime_flexible(None) is None  # type: ignore[arg-type]

    def test_empty_string_returns_none(self):
        assert parse_datetime_flexible("") is None

    def test_garbage_returns_none(self):
        assert parse_datetime_flexible("not a date at all!!!") is None

    def test_iso_with_timezone_returns_naive(self):
        result = parse_datetime_flexible("2026-06-15T10:00:00+05:30")
        assert result is not None
        assert result.tzinfo is None
