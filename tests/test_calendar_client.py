"""Tests for GoogleCalendarClient (docs/design/04-integration-spec.md §3.1).

Covers the four API method bodies and the shared retry helper. Mirrors the
mocking idiom used in ``test_sheet_sync.py``/``test_my_status_writeback.py``:
a ``MagicMock()`` standing in for the discovery ``Resource``, with
``HttpError(resp=MagicMock(status=...), content=b"...")`` simulating
transport failures. Does not modify ``tests/conftest.py`` — the fake service
fixture lives here only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from placement_mail_tracker.calendar_sync import client as calendar_client_module
from placement_mail_tracker.calendar_sync.client import GoogleCalendarClient


def _http_error(status: int) -> HttpError:
    return HttpError(resp=MagicMock(status=status), content=b"error")


@pytest.fixture()
def fake_calendar_service() -> MagicMock:
    """A MagicMock standing in for the ``calendar`` v3 discovery Resource.

    Attribute access on a MagicMock is memoized, so
    ``service.events().insert(...)`` always resolves through the same
    ``service.events().insert`` mock — letting tests configure/assert on it
    directly without re-wiring the chain.
    """
    return MagicMock()


@pytest.fixture()
def calendar_client(mock_settings, fake_calendar_service) -> GoogleCalendarClient:
    return GoogleCalendarClient(mock_settings, service=fake_calendar_service)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every test in this module runs the real retry helper; never actually sleep."""
    monkeypatch.setattr(calendar_client_module.time, "sleep", lambda _seconds: None)


class TestEnsureCalendar:
    def test_finds_existing_calendar_without_creating_duplicate(
        self, calendar_client, fake_calendar_service
    ):
        fake_calendar_service.calendarList().list().execute.return_value = {
            "items": [
                {"id": "cal_other", "summary": "Other Calendar"},
                {"id": "cal_vit", "summary": "VIT Placements"},
            ]
        }

        calendar_id = calendar_client.ensure_calendar("VIT Placements")

        assert calendar_id == "cal_vit"
        fake_calendar_service.calendars().insert.assert_not_called()

    def test_creates_when_absent(self, calendar_client, fake_calendar_service):
        fake_calendar_service.calendarList().list().execute.return_value = {
            "items": [{"id": "cal_other", "summary": "Other Calendar"}]
        }
        fake_calendar_service.calendars().insert().execute.return_value = {
            "id": "cal_new"
        }

        calendar_id = calendar_client.ensure_calendar("VIT Placements")

        assert calendar_id == "cal_new"
        _, kwargs = fake_calendar_service.calendars().insert.call_args
        assert kwargs["body"]["summary"] == "VIT Placements"
        assert kwargs["body"]["timeZone"] == calendar_client.settings.calendar_timezone


class TestInsertEvent:
    def test_returns_new_event_id(self, calendar_client, fake_calendar_service):
        fake_calendar_service.events().insert().execute.return_value = {"id": "evt_1"}
        body = {
            "summary": "Microsoft — OA",
            "extendedProperties": {
                "private": {"drive_id": "MS_1", "opportunity_id": "1"}
            },
        }

        event_id = calendar_client.insert_event("cal_vit", body)

        assert event_id == "evt_1"
        _, kwargs = fake_calendar_service.events().insert.call_args
        assert kwargs["calendarId"] == "cal_vit"
        assert kwargs["body"] is body


class TestPatchEvent:
    def test_patches_by_exact_event_id_not_search(self, calendar_client, fake_calendar_service):
        body = {"summary": "Microsoft — OA (rescheduled)"}

        calendar_client.patch_event("cal_vit", "evt_stored_id", body)

        fake_calendar_service.events().list.assert_not_called()
        fake_calendar_service.events().search.assert_not_called()
        _, kwargs = fake_calendar_service.events().patch.call_args
        assert kwargs["calendarId"] == "cal_vit"
        assert kwargs["eventId"] == "evt_stored_id"
        assert kwargs["body"] is body


class TestGetEvent:
    def test_returns_none_on_404(self, calendar_client, fake_calendar_service):
        fake_calendar_service.events().get().execute.side_effect = _http_error(404)

        result = calendar_client.get_event("cal_vit", "evt_missing")

        assert result is None
        # A 404 is an expected "does not exist" signal, not a failure.
        assert calendar_client.last_error is None
        # No retries should have been spent on an expected 404.
        assert fake_calendar_service.events().get().execute.call_count == 1

    def test_reraises_other_http_errors_after_retries_exhausted(
        self, calendar_client, fake_calendar_service
    ):
        fake_calendar_service.events().get().execute.side_effect = _http_error(500)

        with pytest.raises(HttpError):
            calendar_client.get_event("cal_vit", "evt_1")

        assert fake_calendar_service.events().get().execute.call_count == 3
        assert calendar_client.last_error is not None

    def test_returns_event_on_success(self, calendar_client, fake_calendar_service):
        fake_calendar_service.events().get().execute.return_value = {
            "id": "evt_1",
            "summary": "Microsoft — OA",
        }

        result = calendar_client.get_event("cal_vit", "evt_1")

        assert result == {"id": "evt_1", "summary": "Microsoft — OA"}


class TestRetryHelper:
    def test_retries_on_500_then_succeeds_on_third_attempt(
        self, calendar_client, fake_calendar_service
    ):
        fake_calendar_service.events().insert().execute.side_effect = [
            _http_error(500),
            _http_error(500),
            {"id": "evt_after_retries"},
        ]

        event_id = calendar_client.insert_event("cal_vit", {"summary": "x"})

        assert event_id == "evt_after_retries"
        assert fake_calendar_service.events().insert().execute.call_count == 3
        assert calendar_client.last_error is None

    def test_gives_up_after_three_attempts_and_records_last_error(
        self, calendar_client, fake_calendar_service
    ):
        fake_calendar_service.events().insert().execute.side_effect = _http_error(503)

        with pytest.raises(HttpError):
            calendar_client.insert_event("cal_vit", {"summary": "x"})

        assert fake_calendar_service.events().insert().execute.call_count == 3
        assert calendar_client.last_error is not None

    def test_non_retryable_error_fails_fast_without_sleeping(
        self, calendar_client, fake_calendar_service, monkeypatch
    ):
        sleep_calls: list[float] = []
        monkeypatch.setattr(calendar_client_module.time, "sleep", sleep_calls.append)
        fake_calendar_service.events().insert().execute.side_effect = _http_error(400)

        with pytest.raises(HttpError):
            calendar_client.insert_event("cal_vit", {"summary": "x"})

        assert fake_calendar_service.events().insert().execute.call_count == 1
        assert sleep_calls == []


class TestScopedDeleteCapability:
    """ADR Decision 2 said disappearance is handled by retitling, never
    deletion. docs/design/16 narrowly overrides that, by explicit user
    choice, for confidently-excluded rounds only (a roster explicitly
    proves the student isn't shortlisted) -- calendar_sync/sync.py's
    ``_delete_and_freeze``. The stale pass (a vanished/terminal drive) is
    unaffected and still only retitles; see
    test_calendar_sync.py::test_terminal_drive_grace_period_then_retitle_cancelled
    for that half of the guard.
    """

    def test_deletes_by_exact_event_id_not_search(self, calendar_client, fake_calendar_service):
        calendar_client.delete_event("cal_vit", "evt_stored_id")

        fake_calendar_service.events().list.assert_not_called()
        fake_calendar_service.events().search.assert_not_called()
        _, kwargs = fake_calendar_service.events().delete.call_args
        assert kwargs["calendarId"] == "cal_vit"
        assert kwargs["eventId"] == "evt_stored_id"

    @pytest.mark.parametrize("status", [404, 410])
    def test_treats_404_and_410_as_success(
        self, calendar_client, fake_calendar_service, status
    ):
        """A 404/410 on delete means the goal is already achieved (the user
        deleted it themselves, or a retry after a prior success) -- not an
        error to surface or retry."""
        fake_calendar_service.events().delete().execute.side_effect = _http_error(status)

        calendar_client.delete_event("cal_vit", "evt_missing")  # must not raise
        assert calendar_client.last_error is None
