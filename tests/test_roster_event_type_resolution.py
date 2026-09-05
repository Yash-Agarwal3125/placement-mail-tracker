"""_resolve_roster_event_type: which round (if any) an email is roster
evidence for. Pure classmethod, no fixtures needed."""

from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner


def test_oa_update_maps_directly():
    assert PlacementTrackerRunner._resolve_roster_event_type("OA_UPDATE", {}) == "OA"


def test_interview_update_maps_directly():
    assert (
        PlacementTrackerRunner._resolve_roster_event_type("INTERVIEW_UPDATE", {}) == "INTERVIEW"
    )


def test_shortlist_update_with_only_oa_date_resolves_to_oa():
    opp_data = {"oa_date": "17-Jun-2026 05:30 PM", "interview_date": None}
    assert PlacementTrackerRunner._resolve_roster_event_type("SHORTLIST_UPDATE", opp_data) == "OA"


def test_shortlist_update_with_only_interview_date_resolves_to_interview():
    opp_data = {"oa_date": None, "interview_date": "20-Jun-2026 10:00 AM"}
    assert (
        PlacementTrackerRunner._resolve_roster_event_type("SHORTLIST_UPDATE", opp_data)
        == "INTERVIEW"
    )


def test_shortlist_update_with_both_dates_is_ambiguous_and_skipped():
    opp_data = {"oa_date": "17-Jun-2026 05:30 PM", "interview_date": "20-Jun-2026 10:00 AM"}
    assert PlacementTrackerRunner._resolve_roster_event_type("SHORTLIST_UPDATE", opp_data) is None


def test_shortlist_update_with_neither_date_is_skipped():
    assert PlacementTrackerRunner._resolve_roster_event_type("SHORTLIST_UPDATE", {}) is None


def test_other_classifications_are_skipped():
    assert PlacementTrackerRunner._resolve_roster_event_type("OFFER_UPDATE", {}) is None
