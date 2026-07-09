"""Tests for scheduler/calendar_flags_store.py (docs/design/04-integration-spec.md §4 point 8)."""

from __future__ import annotations

import pytest

from placement_mail_tracker.scheduler import calendar_flags_store


@pytest.fixture(autouse=True)
def _isolate_flags_file(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_flags_store, "_FLAGS_FILE", tmp_path / "calendar_flags.json")


def test_append_then_pop_roundtrip():
    calendar_flags_store.append_calendar_flags(["Acme: OA date became empty"])

    result = calendar_flags_store.pop_pending_calendar_flags()

    assert result == ["Acme: OA date became empty"]
    assert calendar_flags_store.pop_pending_calendar_flags() == []


def test_append_dedupes_against_existing():
    calendar_flags_store.append_calendar_flags(["dup line"])
    calendar_flags_store.append_calendar_flags(["dup line", "new line"])

    result = calendar_flags_store.pop_pending_calendar_flags()

    assert result == ["dup line", "new line"]


def test_append_empty_list_is_noop():
    calendar_flags_store.append_calendar_flags([])

    assert calendar_flags_store.pop_pending_calendar_flags() == []
