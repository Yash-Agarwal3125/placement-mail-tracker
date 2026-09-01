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
    # HIGH priority (not APPLIED -- applying now excludes the deadline
    # event outright, see TestDeadlineGatedOnDemonstratedInterest) admits
    # this deadline so the all-day shape can be checked on its own.
    opp = _opp(deadline="15 June 2026", priority="HIGH")
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


def test_applied_only_mode_gates_oa_interview_and_deadline_when_not_applied(mock_settings):
    """calendar_sync_mode's applied_only gate is independent of, and
    composes with, Phase 7's deadline-interest gate: a NOT_APPLIED/MEDIUM
    drive gets nothing at all under applied_only mode."""
    settings = mock_settings.model_copy(update={"calendar_sync_mode": "applied_only"})
    opp_not_applied = _opp(
        id=1,
        deadline="15 June 2026",
        oa_date="17-Jun-2026 05:30 PM",
        interview_date="20-Jun-2026 10:00 AM",
        my_status="NOT_APPLIED",
    )
    events, _ = derive_events([opp_not_applied], settings)
    assert events == []

    opp_applied = _opp(
        id=2,
        deadline="15 June 2026",
        oa_date="17-Jun-2026 05:30 PM",
        interview_date="20-Jun-2026 10:00 AM",
        my_status="APPLIED",
    )
    events, _ = derive_events([opp_applied], settings)
    event_types = {e.event_type for e in events}
    # DEADLINE is correctly absent here -- once applied, its reminder has
    # done its job (TestDeadlineGatedOnDemonstratedInterest), independent
    # of the applied_only mode gate this test is actually about.
    assert event_types == {"OA", "INTERVIEW"}


def test_all_eligible_mode_includes_oa_interview_regardless_of_my_status(mock_settings):
    """all_eligible mode bypasses the OA/INTERVIEW my_status gate even while
    my_status stays NOT_APPLIED -- DEADLINE is admitted here via HIGH
    priority (Phase 7's other admission path), kept deliberately independent
    of my_status so this test still proves the OA/INTERVIEW mode bypass."""
    settings = mock_settings.model_copy(update={"calendar_sync_mode": "all_eligible"})
    opp = _opp(
        deadline="15 June 2026",
        oa_date="17-Jun-2026 05:30 PM",
        interview_date="20-Jun-2026 10:00 AM",
        my_status="NOT_APPLIED",
        priority="HIGH",
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
    # NOT_ELIGIBLE stays fully silent (working as intended), but an "Unknown"
    # company with a real pending date is flagged rather than silently
    # dropped forever -- see derive_events' comment: this is the exact shape
    # of bug that let a confirmed interview vanish from the calendar after a
    # duplicate-merge left company_name as "Unknown" with no way back.
    assert len(anomalies) == 1
    assert "opportunity_id=2" in anomalies[0]
    assert "Unknown" in anomalies[0]


def test_stopword_fragment_company_name_produces_zero_events(mock_settings):
    """derive.py used to carry its own, narrower copy of the "is this a real
    company" gate than scheduler.runner's -- a garbled subject-line fragment
    like "Is Scheduled On" (the collision-magnet incident) would pass this
    module's gate even after runner.py's own was fixed, and leak onto the
    user's real Google Calendar. Now both share extraction.rule_engine's
    single gate, so this is excluded here too."""
    opp = _opp(id=3, deadline="16 June 2026", company_name="Is Scheduled On")
    events, anomalies = derive_events([opp], mock_settings)
    assert events == []
    assert len(anomalies) == 1
    assert "opportunity_id=3" in anomalies[0]


def test_non_placement_drive_kind_produces_zero_events(mock_settings):
    """docs/design/16 Phase 6: a hackathon/scholarship must never reach the
    calendar even when ELIGIBLE and even for the DEADLINE branch, which the
    calendar_sync_mode gate below never covered."""
    hackathon = _opp(
        id=1, deadline="15 June 2026", drive_kind="HACKATHON", eligibility_status="ELIGIBLE"
    )
    scholarship = _opp(
        id=2, deadline="16 June 2026", drive_kind="SCHOLARSHIP", eligibility_status="ELIGIBLE"
    )
    events, anomalies = derive_events([hackathon, scholarship], mock_settings)
    assert events == []
    assert anomalies == []


def test_missing_drive_kind_defaults_to_placement(mock_settings):
    """Legacy rows without drive_kind populated must keep working (backfill
    default is PLACEMENT at the DB layer; derive_events mirrors that)."""
    opp = _opp(id=1, deadline="15 June 2026", priority="HIGH")
    opp.pop("drive_kind", None)
    events, _ = derive_events([opp], mock_settings)
    assert len(events) == 1


def test_not_matched_roster_verdict_cascades_to_later_round(mock_settings):
    """Cause 2 / Phase 3: selection is a ladder -- a NOT_MATCHED verdict at
    OA now cascades forward and also suppresses INTERVIEW, since you cannot
    reach round n+1 of a drive you were cut from at round n (this replaces
    the old "round independence" behaviour, which was itself the Flipkart
    opp 22 bug: OA excluded but INTERVIEW still derived)."""
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", interview_date="20-Jun-2026", my_status="APPLIED")
    verdicts = {
        (1, "OA"): {"verdict": "NOT_MATCHED", "method": "registration_no"},
    }
    events, _ = derive_events([opp], mock_settings, verdicts)
    types = {e.event_type for e in events}
    assert "OA" not in types
    assert "INTERVIEW" not in types
    assert "DEADLINE" not in types  # no deadline set on this fixture


def test_direct_matched_at_later_round_overrides_inherited_exclusion(mock_settings):
    """The "shortlisted after all" correction path: a direct MATCHED at
    INTERVIEW wins over an inherited NOT_MATCHED-at-OA exclusion."""
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", interview_date="20-Jun-2026", my_status="APPLIED")
    verdicts = {
        (1, "OA"): {"verdict": "NOT_MATCHED", "method": "registration_no"},
        (1, "INTERVIEW"): {"verdict": "MATCHED", "method": "registration_no"},
    }
    events, _ = derive_events([opp], mock_settings, verdicts)
    types = {e.event_type for e in events}
    assert "OA" not in types
    assert "INTERVIEW" in types


def test_not_matched_at_oa_does_not_exclude_oa_itself_twice_or_error(mock_settings):
    """A NOT_MATCHED verdict on the earliest round only has itself to
    exclude directly -- no earlier round exists, so the cascade loop is a
    no-op and this must not raise."""
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", my_status="APPLIED")
    verdicts = {(1, "OA"): {"verdict": "NOT_MATCHED", "method": "registration_no"}}
    events, _ = derive_events([opp], mock_settings, verdicts)
    assert not any(e.event_type == "OA" for e in events)


def test_ambiguous_roster_verdict_does_not_exclude(mock_settings):
    """An AMBIGUOUS verdict (roster didn't parse) must not hide the round --
    unproven exclusion is not the same as proven exclusion (doc 15 §3.3)."""
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", my_status="APPLIED")
    verdicts = {(1, "OA"): {"verdict": "AMBIGUOUS", "method": "none"}}
    events, _ = derive_events([opp], mock_settings, verdicts)
    assert any(e.event_type == "OA" for e in events)


def test_no_roster_verdict_at_all_does_not_exclude(mock_settings):
    """Never-evaluated (no row) behaves exactly as it does today."""
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", my_status="APPLIED")
    events, _ = derive_events([opp], mock_settings, {})
    assert any(e.event_type == "OA" for e in events)


def test_collision_guard_drops_higher_opportunity_id(mock_settings):
    opp1 = _opp(id=1, company_name="Acme Corp", deadline="15 June 2026", priority="HIGH")
    opp2 = _opp(id=2, company_name="Acme Corp", deadline="15 June 2026", priority="HIGH")
    events, anomalies = derive_events([opp1, opp2], mock_settings)

    deadline_events = [e for e in events if e.event_type == "DEADLINE"]
    assert len(deadline_events) == 1
    assert deadline_events[0].opportunity_id == 1
    assert len(anomalies) == 1
    assert "opportunity_id=2" in anomalies[0]


class TestDeadlineGatedOnDemonstratedInterest:
    """Phase 7 (calendar-drift remediation plan, Cause 5), revised
    2026-08-27 per explicit user request: a DEADLINE event is only emitted
    for a NOT_APPLIED, HIGH-priority drive -- not yet applied, but worth a
    nudge to. The moment my_status shows real engagement (APPLIED or
    further -- SHORTLISTED/SELECTED/REJECTED/...), the "apply by" reminder
    has done its job and is now excluded outright, regardless of priority:
    the user already gets an application-confirmation email for that drive,
    and a deadline reminder surviving past that point is just confusion,
    not a nudge. (Originally this gate only *added* a reason to show the
    event once engaged; that direction is now inverted -- engagement is a
    reason to hide it.) Still fixes the original Spense complaint too: an
    ELIGIBLE, NOT_APPLIED, MEDIUM-priority drive with no evidence of
    registration still gets no calendar deadline."""

    def test_not_applied_medium_priority_gets_no_deadline_event(self, mock_settings):
        opp = _opp(deadline="15 June 2026", my_status="NOT_APPLIED", priority="MEDIUM")
        events, anomalies = derive_events([opp], mock_settings)
        assert events == []
        assert anomalies == []

    def test_high_priority_gets_deadline_event_despite_not_applied(self, mock_settings):
        opp = _opp(deadline="15 June 2026", my_status="NOT_APPLIED", priority="HIGH")
        events, _ = derive_events([opp], mock_settings)
        deadline_events = [e for e in events if e.event_type == "DEADLINE"]
        assert len(deadline_events) == 1

    def test_applied_excludes_deadline_event_even_at_high_priority(self, mock_settings):
        """The reminder's job is done once you've actually applied --
        confirmed by the user's own application-confirmation email -- so
        this overrides HIGH priority rather than the other way around."""
        opp = _opp(deadline="15 June 2026", my_status="APPLIED", priority="HIGH")
        events, _ = derive_events([opp], mock_settings)
        assert not any(e.event_type == "DEADLINE" for e in events)

    def test_shortlisted_and_selected_also_exclude_the_deadline_event(self, mock_settings):
        for status in ("SHORTLISTED", "SELECTED"):
            opp = _opp(deadline="15 June 2026", my_status=status, priority="HIGH")
            events, _ = derive_events([opp], mock_settings)
            assert not any(e.event_type == "DEADLINE" for e in events), (
                f"expected no deadline event for {status}"
            )

    def test_missing_my_status_treated_as_not_applied(self, mock_settings):
        opp = _opp(deadline="15 June 2026", priority="LOW")
        opp.pop("my_status", None)
        events, _ = derive_events([opp], mock_settings)
        assert events == []

    def test_oa_and_interview_derivation_unaffected_by_deadline_gate(self, mock_settings):
        """OA/INTERVIEW gating must stay governed purely by the existing
        roster/eligibility (calendar_sync_mode) logic -- untouched here."""
        settings = mock_settings.model_copy(update={"calendar_sync_mode": "all_eligible"})
        opp = _opp(
            oa_date="17-Jun-2026 05:30 PM",
            interview_date="20-Jun-2026 10:00 AM",
            my_status="NOT_APPLIED",
            priority="LOW",
        )
        events, _ = derive_events([opp], settings)
        event_types = {e.event_type for e in events}
        assert event_types == {"OA", "INTERVIEW"}
        assert "DEADLINE" not in event_types


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

    changed_color = event.model_copy(update={"color_id": "11"})
    assert changed_color.content_hash() != event.content_hash()


def test_ppt_event_derived_for_applied_drive(mock_settings):
    """2026-08-23/24 gap: a pre-placement talk has its own date field and
    calendar event type now, gated on 'applied' only (never roster-gated --
    a PPT is open to every applicant, not a shortlisted subset)."""
    opp = _opp(ppt_date="24 August 2026 3:30 PM", my_status="APPLIED")
    events, _ = derive_events([opp], mock_settings)
    ppt_events = [e for e in events if e.event_type == "PPT"]
    assert len(ppt_events) == 1
    assert ppt_events[0].color_id is None


def test_ppt_event_not_derived_when_not_applied(mock_settings):
    opp = _opp(ppt_date="24 August 2026 3:30 PM", my_status="NOT_APPLIED")
    events, _ = derive_events([opp], mock_settings)
    assert not any(e.event_type == "PPT" for e in events)


def test_ppt_event_not_excluded_by_a_not_matched_oa_verdict(mock_settings):
    """PPT sits outside ROUND_ORDER -- is_round_excluded never applies to
    it, so it must survive even when OA is roster-excluded on the same
    drive."""
    opp = _opp(
        oa_date="17-Jun-2026 05:30 PM", ppt_date="24 August 2026 3:30 PM", my_status="APPLIED"
    )
    verdicts = {(1, "OA"): {"verdict": "NOT_MATCHED", "method": "registration_no"}}
    events, _ = derive_events([opp], mock_settings, verdicts)
    types = {e.event_type for e in events}
    assert "OA" not in types
    assert "PPT" in types


def test_oa_event_flagged_red_without_a_matched_verdict(mock_settings):
    """User-requested behavior: an OA/INTERVIEW event that isn't excluded
    but also isn't positively confirmed by a MATCHED roster verdict still
    shows (doc 15 §3.3 -- unproven exclusion must not hide it), but gets the
    unproven color instead of the calendar's default."""
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", my_status="APPLIED")
    events, _ = derive_events([opp], mock_settings)  # no roster_verdicts at all
    oa_event = next(e for e in events if e.event_type == "OA")
    assert oa_event.color_id == "11"


def test_oa_event_not_flagged_red_with_a_matched_verdict(mock_settings):
    opp = _opp(oa_date="17-Jun-2026 05:30 PM", my_status="APPLIED")
    verdicts = {(1, "OA"): {"verdict": "MATCHED", "method": "codename"}}
    events, _ = derive_events([opp], mock_settings, verdicts)
    oa_event = next(e for e in events if e.event_type == "OA")
    assert oa_event.color_id is None


def test_deadline_event_never_gets_the_unproven_color(mock_settings):
    """DEADLINE is never roster-gated at all -- it must never pick up the
    unproven-red color regardless of any OA/INTERVIEW verdict state. Uses
    NOT_APPLIED/HIGH priority to admit the event at all -- APPLIED now
    excludes it outright (TestDeadlineGatedOnDemonstratedInterest)."""
    opp = _opp(deadline="15 June 2026", my_status="NOT_APPLIED", priority="HIGH")
    events, _ = derive_events([opp], mock_settings)
    deadline_event = next(e for e in events if e.event_type == "DEADLINE")
    assert deadline_event.color_id is None
