"""Feature 2 backlog item 2 (docs/design/05, docs/design/10): morning-of-
event "TODAY" digest section for OA/interviews on drives the user has
actually engaged with."""

from __future__ import annotations

from datetime import datetime

from placement_mail_tracker.scheduler.digest_generator import _format_digest


def test_today_section_lists_oa_and_interview_with_time_and_venue():
    now = datetime(2026, 7, 10, 8, 0, 0)
    today_events = [
        (
            {"company_name": "Cisco", "my_status": "APPLIED", "work_location": "Bengaluru"},
            "OA",
            now.replace(hour=14, minute=30),
        ),
        (
            {"company_name": "Groww", "my_status": "SHORTLISTED"},
            "Interview",
            now.replace(hour=11, minute=0),
        ),
    ]

    output = _format_digest([], [], [], now, today_events=today_events)

    assert "TODAY" in output
    assert "Cisco" in output
    assert "Bengaluru" in output
    assert "Groww" in output
    # Sorted chronologically: Groww (11:00) before Cisco (14:30).
    assert output.index("Groww") < output.index("Cisco")


def test_today_section_omitted_when_empty():
    output = _format_digest([], [], [], datetime.now(), today_events=[])
    assert "TODAY" not in output


def test_today_section_omitted_when_none_passed():
    output = _format_digest([], [], [], datetime.now())
    assert "TODAY" not in output


def test_flagged_deadline_section_lists_unverified_drives():
    now = datetime(2026, 7, 10, 8, 0, 0)
    flagged = [{"company_name": "Flagged Co", "deadline": "15 July"}]

    output = _format_digest([], [], [], now, flagged_deadlines=flagged)

    assert "DEADLINE UNVERIFIED" in output
    assert "Flagged Co" in output
    assert "check manually" in output


def test_flagged_deadline_section_omitted_when_empty():
    output = _format_digest([], [], [], datetime.now(), flagged_deadlines=[])
    assert "DEADLINE UNVERIFIED" not in output


def test_quota_deferred_count_surfaces_in_system_health():
    """Extraction-reliability finding: quota death must be user-visible, not
    silent (docs/design/06-extraction-reliability.md)."""
    output = _format_digest([], [], [], datetime.now(), quota_deferred_count=3)

    assert "SYSTEM HEALTH" in output
    assert "3" in output
    assert "quota" in output.lower()


def test_quota_deferred_count_omitted_when_zero():
    output = _format_digest([], [], [], datetime.now(), quota_deferred_count=0)
    assert "quota" not in output.lower()


def test_system_health_shows_both_dead_letters_and_quota_deferral():
    output = _format_digest(
        [], [], [], datetime.now(), dead_letter_count=2, quota_deferred_count=5,
    )
    assert "SYSTEM HEALTH" in output
    assert "dead letters" in output
    assert "5" in output and "quota" in output.lower()
