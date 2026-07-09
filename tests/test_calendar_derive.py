"""Tests for calendar_sync.derive.derive_events (spec §5, cases 1-8)."""

from __future__ import annotations

from typing import Any

from placement_mail_tracker.calendar_sync.derive import CalendarEvent, derive_events


def _opp(**overrides: Any) -> dict[str, Any]:
    """Build a bare opportunities-row dict with the real DB column names
    consumed by derive_events (spec §3.2 / §1). Deliberately hand-built
    rather than reusing conftest's `sample_opportunity`, which produces the
    extraction-schema shape, not the DB row shape."""
    base: dict[str, Any] = {
        "id": 1,
        "drive_id": "DRIVE-1",
        "company_name": "Microsoft",
        "role": "SDE Intern",
        "deadline": None,
        "oa_date": None,
        "interview_date": None,
        "work_location": "Bangalore",
        "package_or_stipend": "50000/month",
        "action_required": "Apply on portal",
        "current_status": "OPEN",
        "status": "active",
        "eligibility_status": "ELIGIBLE",
        "my_status": "NOT_APPLIED",
        "source_thread_id": "thread-123",
        "source_email_id": "email-123",
    }
    base.update(overrides)
    return base


def test_timed_oa_event_has_offset_and_one_hour_duration(mock_settings):
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", my_status="APPLIED")
    events, anomalies = derive_events([opp], mock_settings)

    oa_events = [e for e in events if e.event_type == "OA"]
    assert len(oa_events) == 1
    event = oa_events[0]
    assert not anomalies
    assert event.all_day is False
    assert event.start_iso.endswith("+05:30")
    assert event.end_iso.endswith("+05:30")
    # 1 hour duration.
    start = event.start_iso
    end = event.end_iso
    assert start[:10] == end[:10]
    assert start[11:13] == "17"
    assert end[11:13] == "18"
    assert event.reminder_minutes == mock_settings.calendar_event_reminder_minutes


def test_date_only_deadline_is_all_day(mock_settings):
    opp = _opp(deadline="15 June 2026")
    events, anomalies = derive_events([opp], mock_settings)

    deadline_events = [e for e in events if e.event_type == "DEADLINE"]
    assert len(deadline_events) == 1
    event = deadline_events[0]
    assert not anomalies
    assert event.all_day is True
    assert event.start_iso == "2026-06-15"
    assert event.end_iso == "2026-06-15"
    assert event.reminder_minutes == mock_settings.calendar_deadline_reminder_minutes


def test_fuzzy_only_garbage_produces_no_event_and_anomaly(mock_settings):
    opp = _opp(deadline="Round 3 at 5 in Lab 2")
    events, anomalies = derive_events([opp], mock_settings)

    assert events == []
    assert len(anomalies) == 1
    assert "Round 3 at 5 in Lab 2" in anomalies[0]
    assert "could not be parsed" in anomalies[0]


def test_bare_year_and_out_of_range_date_produce_no_event_and_anomaly(mock_settings):
    opp1 = _opp(id=1, deadline="2026")
    opp2 = _opp(id=2, deadline="15 June 2099")
    events, anomalies = derive_events([opp1, opp2], mock_settings)

    assert events == []
    assert len(anomalies) == 2


def test_applied_only_mode_gates_oa_interview_but_not_deadline(mock_settings):
    settings = mock_settings.model_copy(update={"calendar_sync_mode": "applied_only"})
    opp_not_applied = _opp(
        id=1,
        deadline="15 June 2026",
        oa_date="17-Jun-2026 05:30 PM",
        interview_date="20-Jun-2026 10:00 AM",
        my_status="NOT_APPLIED",
    )
    events, _ = derive_events([opp_not_applied], settings)
    event_types = {e.event_type for e in events}
    assert event_types == {"DEADLINE"}

    opp_applied = _opp(
        id=2,
        deadline="15 June 2026",
        oa_date="17-Jun-2026 05:30 PM",
        interview_date="20-Jun-2026 10:00 AM",
        my_status="APPLIED",
    )
    events, _ = derive_events([opp_applied], settings)
    event_types = {e.event_type for e in events}
    assert event_types == {"DEADLINE", "OA", "INTERVIEW"}


def test_all_eligible_mode_includes_oa_interview_regardless_of_my_status(mock_settings):
    settings = mock_settings.model_copy(update={"calendar_sync_mode": "all_eligible"})
    opp = _opp(
        deadline="15 June 2026",
        oa_date="17-Jun-2026 05:30 PM",
        interview_date="20-Jun-2026 10:00 AM",
        my_status="NOT_APPLIED",
    )
    events, _ = derive_events([opp], settings)
    event_types = {e.event_type for e in events}
    assert event_types == {"DEADLINE", "OA", "INTERVIEW"}


def test_not_eligible_and_unknown_company_produce_zero_events(mock_settings):
    opp_not_eligible = _opp(
        id=1, deadline="15 June 2026", eligibility_status="NOT_ELIGIBLE_BRANCH"
    )
    opp_unknown_company = _opp(id=2, deadline="16 June 2026", company_name="Unknown")
    events, anomalies = derive_events(
        [opp_not_eligible, opp_unknown_company], mock_settings
    )
    assert events == []
    assert anomalies == []


def test_collision_guard_drops_higher_opportunity_id(mock_settings):
    opp1 = _opp(id=1, company_name="Acme Corp", deadline="15 June 2026")
    opp2 = _opp(id=2, company_name="Acme Corp", deadline="15 June 2026")
    events, anomalies = derive_events([opp1, opp2], mock_settings)

    deadline_events = [e for e in events if e.event_type == "DEADLINE"]
    assert len(deadline_events) == 1
    assert deadline_events[0].opportunity_id == 1
    assert len(anomalies) == 1
    assert "opportunity_id=2" in anomalies[0]


def test_content_hash_stable_and_changes_with_fields():
    event = CalendarEvent(
        opportunity_id=1,
        drive_id="DRIVE-1",
        event_type="DEADLINE",
        title="Microsoft — Apply by deadline",
        start_iso="2026-06-15",
        end_iso="2026-06-15",
        all_day=True,
        location="Bangalore",
        description="desc",
        reminder_minutes=[1440],
    )
    same_event = event.model_copy()
    assert event.content_hash() == same_event.content_hash()

    changed_title = event.model_copy(update={"title": "Microsoft — Different"})
    assert changed_title.content_hash() != event.content_hash()

    changed_start = event.model_copy(update={"start_iso": "2026-06-16"})
    assert changed_start.content_hash() != event.content_hash()

    changed_location = event.model_copy(update={"location": "Chennai"})
    assert changed_location.content_hash() != event.content_hash()
