"""Tests for parse_datetime_flexible date-hardening (FS T1.5) and human_relative_time."""

from __future__ import annotations

from datetime import datetime, timedelta

from placement_mail_tracker.utils.time import (
    _MAX_YEAR_DELTA,
    _MIN_YEAR,
    human_relative_time,
    parse_datetime_flexible,
    parse_datetime_strict,
)


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


class TestStrictDateParsing:
    """parse_datetime_strict: a new, additional parser (fuzzy=False-equivalent,
    explicit format whitelist) used only by the post-extraction validation
    layer (extraction/validation.py) — never a replacement for
    parse_datetime_flexible, which every other consumer keeps using unchanged.
    """

    def test_iso_date_is_accepted(self):
        result = parse_datetime_strict("2026-07-04")
        assert result is not None
        assert (result.year, result.month, result.day) == (2026, 7, 4)

    def test_iso_datetime_with_minutes_is_accepted(self):
        result = parse_datetime_strict("2026-07-01T15:00")
        assert result is not None
        assert (result.hour, result.minute) == (15, 0)

    def test_full_month_name_is_accepted(self):
        result = parse_datetime_strict("17 June 2026")
        assert result is not None
        assert (result.year, result.month, result.day) == (2026, 6, 17)

    def test_abbreviated_month_with_time_is_accepted(self):
        result = parse_datetime_strict("15-Jun-2026 05:30 PM")
        assert result is not None
        assert (result.month, result.day, result.hour) == (6, 15, 17)

    def test_slash_date_is_read_as_day_month_year(self):
        # "04/07/2026" must mean 4 July 2026, not 7 April (the Indian
        # placement-cell DMY convention the Gemini prompt now states explicitly).
        result = parse_datetime_strict("04/07/2026")
        assert result is not None
        assert (result.month, result.day) == (7, 4)

    def test_dash_slash_date_is_read_as_day_month_year(self):
        result = parse_datetime_strict("1-07-2026")
        assert result is not None
        assert (result.month, result.day) == (7, 1)

    def test_fuzzy_only_garbage_is_rejected(self):
        # parse_datetime_flexible's fuzzy mode resolves this to a real-looking
        # date (it extracts "2026" and "June" and fills in today's day) even
        # though the string states no actual date. The strict whitelist
        # requires the *entire* string to match a known format, so it rejects it.
        garbage = "Contact HR at extension 2026 in June"
        assert parse_datetime_flexible(garbage) is not None
        assert parse_datetime_strict(garbage) is None

    def test_free_text_with_numbers_is_rejected(self):
        assert parse_datetime_strict("Round 3 at 5 in Lab 2") is None

    def test_bare_year_is_rejected(self):
        assert parse_datetime_strict("2026") is None

    def test_year_before_min_is_rejected(self):
        assert parse_datetime_strict("1999-06-15") is None

    def test_year_far_in_future_is_rejected(self):
        far_year = datetime.now().year + _MAX_YEAR_DELTA + 1
        assert parse_datetime_strict(f"{far_year}-06-15") is None

    def test_year_at_max_boundary_is_accepted(self):
        max_year = datetime.now().year + _MAX_YEAR_DELTA
        result = parse_datetime_strict(f"{max_year}-06-15")
        assert result is not None
        assert result.year == max_year

    def test_none_input_returns_none(self):
        assert parse_datetime_strict(None) is None  # type: ignore[arg-type]

    def test_empty_string_returns_none(self):
        assert parse_datetime_strict("") is None


class TestHumanRelativeTime:
    def test_none_returns_empty(self):
        assert human_relative_time(None) == ""

    def test_today(self):
        dt = datetime.now().replace(hour=9, minute=5)
        result = human_relative_time(dt)
        assert result.startswith("Today,")

    def test_yesterday(self):
        dt = datetime.now() - timedelta(days=1)
        result = human_relative_time(dt)
        assert result.startswith("Yesterday,")

    def test_days_ago(self):
        dt = datetime.now() - timedelta(days=3)
        assert human_relative_time(dt) == "3 days ago"

    def test_one_week_ago(self):
        dt = datetime.now() - timedelta(days=8)
        assert human_relative_time(dt) == "1 week ago"

    def test_old_date_returns_formatted(self):
        dt = datetime(2024, 3, 15)
        result = human_relative_time(dt)
        assert "Mar 2024" in result
