"""Regression tests for the post-extraction validation layer.

validate_opportunity_data() never raises and never blocks storage — it only
returns human-readable review flags. These tests cover each rule in
isolation and confirm a flagged drive is still written by the runner (not
dropped), matching the fail-soft principle in CLAUDE.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.extraction.validation import (
    LOW_CONFIDENCE_THRESHOLD,
    validate_opportunity_data,
)
from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner


def _opp(**overrides) -> dict:
    base = {
        "deadline": None,
        "oa_date": None,
        "interview_date": None,
        "cgpa_requirement": None,
        "confidence": None,
    }
    base.update(overrides)
    return base


class TestNoFlags:
    def test_empty_opportunity_has_no_flags(self):
        assert validate_opportunity_data(_opp()) == []

    def test_well_formed_future_dates_have_no_flags(self):
        opp = _opp(
            deadline="2026-07-04T10:00",
            interview_date="2026-07-16",
            cgpa_requirement="7.5",
            confidence=0.9,
        )
        flags = validate_opportunity_data(opp, email_received_at="2026-07-03T11:47:50+05:30")
        assert flags == []


class TestPastDatedEvent:
    def test_deadline_before_email_received_is_flagged(self):
        opp = _opp(deadline="2026-06-01")
        flags = validate_opportunity_data(opp, email_received_at="2026-07-03T10:00:00+05:30")
        assert any("dated before the email arrived" in f for f in flags)

    def test_uses_email_received_at_not_wall_clock(self):
        # A backlog run processing old mail must not flag a deadline that was
        # in the future *relative to the email*, even if it's now in the past
        # relative to wall-clock "today".
        opp = _opp(deadline="2020-06-15")
        flags = validate_opportunity_data(opp, email_received_at="2020-06-01T09:00:00")
        assert not any("dated before the email arrived" in f for f in flags)

    def test_no_anchor_falls_back_to_now_and_flags_past_dates(self):
        opp = _opp(deadline="2020-06-15")
        flags = validate_opportunity_data(opp, email_received_at=None)
        assert any("dated before the email arrived" in f for f in flags)


class TestDeadlineAfterInterview:
    def test_illogical_ordering_is_flagged(self):
        opp = _opp(deadline="2026-07-20", interview_date="2026-07-10")
        flags = validate_opportunity_data(opp, email_received_at="2026-07-01T00:00:00")
        assert any("illogical ordering" in f for f in flags)

    def test_normal_ordering_is_not_flagged(self):
        opp = _opp(deadline="2026-07-04", interview_date="2026-07-16")
        flags = validate_opportunity_data(opp, email_received_at="2026-07-01T00:00:00")
        assert not any("illogical ordering" in f for f in flags)

    def test_only_one_date_present_is_not_compared(self):
        opp = _opp(deadline="2026-07-20")
        flags = validate_opportunity_data(opp, email_received_at="2026-07-01T00:00:00")
        assert not any("illogical ordering" in f for f in flags)

    def test_unparseable_date_is_not_compared(self):
        opp = _opp(deadline="not a date", interview_date="2026-07-10")
        flags = validate_opportunity_data(opp, email_received_at="2026-07-01T00:00:00")
        assert not any("illogical ordering" in f for f in flags)


class TestCgpaOutOfRange:
    def test_cgpa_above_ten_is_flagged(self):
        flags = validate_opportunity_data(_opp(cgpa_requirement="15.0"))
        assert any("outside the plausible range" in f for f in flags)

    def test_cgpa_negative_is_flagged(self):
        flags = validate_opportunity_data(_opp(cgpa_requirement="-1"))
        assert any("outside the plausible range" in f for f in flags)

    def test_cgpa_in_range_is_not_flagged(self):
        flags = validate_opportunity_data(_opp(cgpa_requirement="7.5"))
        assert not any("outside the plausible range" in f for f in flags)

    def test_cgpa_boundary_values_are_not_flagged(self):
        assert not any(
            "outside the plausible range" in f
            for f in validate_opportunity_data(_opp(cgpa_requirement="0"))
        )
        assert not any(
            "outside the plausible range" in f
            for f in validate_opportunity_data(_opp(cgpa_requirement="10"))
        )


class TestLowConfidence:
    def test_below_threshold_is_flagged(self):
        opp = _opp(confidence=LOW_CONFIDENCE_THRESHOLD - 0.01)
        flags = validate_opportunity_data(opp)
        assert any("low-confidence" in f for f in flags)

    def test_at_or_above_threshold_is_not_flagged(self):
        opp = _opp(confidence=LOW_CONFIDENCE_THRESHOLD)
        assert not any("low-confidence" in f for f in validate_opportunity_data(opp))

    def test_missing_confidence_is_not_flagged(self):
        # Rule-only extraction never sets "confidence" at all — must not
        # be treated as automatically low-confidence.
        assert not any("low-confidence" in f for f in validate_opportunity_data(_opp()))


class TestStrictParseReject:
    def test_fuzzy_only_garbage_date_is_flagged(self):
        opp = _opp(oa_date="Contact HR at extension 2026 in June")
        flags = validate_opportunity_data(opp, email_received_at="2026-06-01T00:00:00")
        assert any("only parses under fuzzy date matching" in f for f in flags)

    def test_clean_iso_date_is_not_flagged_as_garbage(self):
        opp = _opp(oa_date="2026-07-01T15:00")
        flags = validate_opportunity_data(opp, email_received_at="2026-06-30T00:00:00")
        assert not any("only parses under fuzzy date matching" in f for f in flags)

    def test_ambiguous_slash_date_disagreement_is_flagged(self):
        # "04/07/2026" parses to two DIFFERENT dates depending on convention:
        # flexible (MM/DD default) reads 7 April; the strict DD/MM whitelist
        # reads 4 July. Both "succeed", which is worse than an outright
        # rejection, so this gets its own flag distinct from the garbage case.
        opp = _opp(deadline="04/07/2026")
        flags = validate_opportunity_data(opp, email_received_at="2026-06-01T00:00:00")
        assert any("is ambiguous" in f for f in flags)


class TestFlaggedDrivesAreStillWritten:
    """Integration-style check: validate_opportunity_data() output is meant
    to be stored as-is (never used to veto insert_or_update_opportunity)."""

    def test_flagged_opportunity_still_inserts(self, db_manager):
        opp = {
            "company_name": "Acme",
            "role": "Intern",
            "internship_or_fulltime": "internship",
            "package_or_stipend": "50000",
            "eligibility": None,
            "cgpa_requirement": "15.0",  # implausible
            "branches_allowed": ["CSE"],
            "deadline": "2020-01-01",  # implausible (far past)
            "interview_date": None,
            "oa_date": None,
            "registration_link": None,
            "work_location": "Remote",
            "hiring_process": [],
            "important_notes": [],
            "current_status": "OPEN",
        }
        flags = validate_opportunity_data(opp, email_received_at="2026-07-01T00:00:00")
        assert flags  # this fixture is deliberately implausible
        opp["validation_flags"] = flags

        opp_id, created = db_manager.insert_or_update_opportunity(
            opp, source_email_id="acme_001"
        )
        assert created is True
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record is not None
        assert record["company_name"] == "Acme"
        assert record["validation_flags"] == flags

    def test_map_extraction_to_opportunity_threads_confidence(self):
        from placement_mail_tracker.scheduler.runner import map_extraction_to_opportunity

        extraction = {"company_name": "Acme", "confidence": 0.2}
        opp_data = map_extraction_to_opportunity(extraction)
        assert opp_data["confidence"] == 0.2

        flags = validate_opportunity_data(_opp(confidence=opp_data["confidence"]))
        assert any("low-confidence" in f for f in flags)


class TestRunnerWiring:
    """End-to-end (via PlacementTrackerRunner._process_single_message) checks
    that the validation layer is actually wired in, not just unit-testable
    in isolation."""

    def test_low_confidence_implausible_drive_is_still_stored_and_flagged(
        self, db_manager, mock_settings
    ):
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)

        extractor = MagicMock()
        extractor.extract_from_email.return_value = {
            "company_name": "FlagCo",
            "role": "Intern",
            "cgpa_requirement": "15",  # implausible: > 10
            "confidence": 0.2,  # below LOW_CONFIDENCE_THRESHOLD
        }

        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        msg = {
            "message_id": "flagged_msg",
            "thread_id": "thread_flagged",
            "subject": "Internship registration is now open",
            "sender": "cdc@college.edu",
            "body_text": "Apply via the portal.",
            "timestamp": "2026-07-03T10:00:00+05:30",
        }

        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        assert stats["created"] == 1  # the drive was written, not dropped
        # fetch_opportunity_by_thread_id returns a raw sqlite3.Row (JSON_FIELDS
        # are only deserialized to lists by fetch_opportunity_by_id /
        # fetch_active_*); substring-check the raw JSON text directly.
        drive = db_manager.fetch_opportunity_by_thread_id("thread_flagged")
        assert drive is not None
        assert drive["company_name"].lower() == "flagco"
        assert "outside the plausible range" in drive["validation_flags"]
        assert "low-confidence" in drive["validation_flags"]

    def test_stale_flag_survives_a_followup_that_omits_the_date(
        self, db_manager, mock_settings
    ):
        """A follow-up that doesn't restate oa_date must not silently reset
        validation_flags to "looks fine" while the implausible stored date is
        still sitting untouched in the row (see runner.py's effective_dates
        merge, added specifically to close this gap)."""
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)

        extractor = MagicMock()
        extractor.extract_from_email.return_value = {
            "company_name": "StaleFlagCo",
            "role": "Intern",
            "oa_date": "2026-01-15",  # well before the email's own timestamp
        }
        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        first_msg = {
            "message_id": "stale_msg_1",
            "thread_id": "thread_stale",
            "subject": "Internship registration is now open",
            "sender": "cdc@college.edu",
            "body_text": "Apply via the portal.",
            "timestamp": "2026-07-03T10:00:00+05:30",
        }
        runner._process_single_message(first_msg, db_manager, extractor, UserProfile.load(), stats)

        drive = db_manager.fetch_opportunity_by_thread_id("thread_stale")
        assert "dated before the email arrived" in drive["validation_flags"]

        # Follow-up that never mentions oa_date at all, and must not call
        # Gemini (known-thread status-only follow-up — the existing cost guard).
        extractor.extract_from_email.side_effect = AssertionError("Gemini was called!")
        follow_up = {
            "message_id": "stale_msg_2",
            "thread_id": "thread_stale",
            "subject": "Shortlisted for next round",
            "sender": "cdc@college.edu",
            "body_text": "You have been shortlisted for the next round.",
            "timestamp": "2026-07-05T09:00:00+05:30",
        }
        runner._process_single_message(follow_up, db_manager, extractor, UserProfile.load(), stats)

        updated_drive = db_manager.fetch_opportunity_by_thread_id("thread_stale")
        assert updated_drive["oa_date"] == "2026-01-15"  # COALESCE preserved it
        assert "dated before the email arrived" in updated_drive["validation_flags"], (
            "stale implausible date must stay flagged, not silently reset to []"
        )
