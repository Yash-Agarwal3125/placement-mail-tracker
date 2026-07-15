"""Tests for the project-evolution pass.

Covers behaviour that was previously broken or dormant:
- action_required now understands human-formatted dates (not just ISO)
- next_event_date is derived from extracted OA/interview dates
- dashboard "this week" metrics are genuinely date-bounded
- the Gmail fetch window only advances when the fetch actually succeeds
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from placement_mail_tracker.ai.gemini_extractor import GeminiQuotaExhaustedError
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.scheduler.alert_generator import AlertGenerator
from placement_mail_tracker.scheduler.runner import (
    _RETRY_MAX,
    PlacementTrackerRunner,
    _derive_next_event_date,
    _is_identifiable_company,
    canonicalize_status,
)

# ---------------------------------------------------------------------------
# action_required understands human dates
# ---------------------------------------------------------------------------


class TestActionRequiredHumanDates:
    def test_apply_today_with_human_deadline(self, db_manager: DatabaseManager):
        # "17 June 2026"-style strings used to silently fail datetime.fromisoformat
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d %B %Y")
        opp = {
            "company_name": "HumanDate Co",
            "role": "Intern",
            "internship_or_fulltime": "internship",
            "deadline": tomorrow,
            "current_status": "OPEN",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="hd_1")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] == "APPLY TODAY"

    def test_prepare_for_test_with_dmy_format(self, db_manager: DatabaseManager):
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d-%b-%Y %I:%M %p")
        opp = {
            "company_name": "DMY Co",
            "role": "Engineer",
            "internship_or_fulltime": "full_time",
            "oa_date": tomorrow,
            "current_status": "OA",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="hd_2")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] == "PREPARE FOR TEST"


# ---------------------------------------------------------------------------
# next_event_date derivation
# ---------------------------------------------------------------------------


class TestDeriveNextEventDate:
    def test_prefers_soonest_upcoming_event(self):
        now = datetime.now()
        oa = (now + timedelta(days=5)).isoformat()
        interview = (now + timedelta(days=2)).isoformat()
        result = _derive_next_event_date({"oa_date": oa, "interview_date": interview})
        assert result == interview

    def test_none_when_no_event_dates(self):
        assert _derive_next_event_date({"deadline": "15 June 2099"}) is None

    def test_falls_back_to_past_event_when_no_upcoming(self):
        past = (datetime.now() - timedelta(days=3)).isoformat()
        assert _derive_next_event_date({"oa_date": past}) == past


# ---------------------------------------------------------------------------
# Dashboard metrics are date-bounded
# ---------------------------------------------------------------------------


class TestDashboardWeekMetrics:
    def test_only_counts_events_within_a_week(
        self, db_manager: DatabaseManager, sample_opportunity
    ):
        soon = (datetime.now() + timedelta(days=2)).isoformat()
        far = (datetime.now() + timedelta(days=40)).isoformat()
        db_manager.insert_or_update_opportunity(
            sample_opportunity("Soon Co", "R1", current_status="OA", oa_date=soon),
            source_email_id="dash_soon",
        )
        db_manager.insert_or_update_opportunity(
            sample_opportunity("Far Co", "R2", current_status="OA", oa_date=far),
            source_email_id="dash_far",
        )
        metrics = db_manager.get_dashboard_metrics()
        assert metrics["oa_this_week"] == 1
        assert "deadlines_this_week" in metrics
        assert "action_required" in metrics


# ---------------------------------------------------------------------------
# Fetch window only advances on a successful Gmail fetch
# ---------------------------------------------------------------------------


@pytest.fixture
def runner_settings(tmp_path):
    return Settings(
        app_env="testing",
        fetch_state_file=str(tmp_path / "fetch_state.json"),
    )


class TestGeminiCostGuard:
    """A status-only follow-up on a known thread updates via rules without
    calling Gemini — except OA_UPDATE/INTERVIEW_UPDATE, which always need it."""

    def test_known_thread_followup_skips_gemini(
        self, db_manager: DatabaseManager, mock_settings, sample_opportunity
    ):
        # Seed an existing drive tied to a Gmail thread.
        db_manager.insert_or_update_opportunity(
            sample_opportunity("Microsoft", "Software Engineer Intern"),
            source_email_id="orig_msg",
            source_thread_id="thread_known",
        )

        runner = PlacementTrackerRunner(
            connection=db_manager.connection, settings=mock_settings
        )

        # Extractor that explodes if Gemini is ever called.
        extractor = MagicMock()
        extractor.extract_from_email.side_effect = AssertionError("Gemini was called!")

        from placement_mail_tracker.config.user_profile import UserProfile

        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        followup = {
            "message_id": "followup_msg",
            "thread_id": "thread_known",
            "subject": "Shortlisted for next round",
            "sender": "cdc@college.edu",
            "body_text": "You have been shortlisted for the next round.",
            "timestamp": datetime.now().isoformat(),
        }

        runner._process_single_message(
            followup, db_manager, extractor, UserProfile.load(), stats
        )

        extractor.extract_from_email.assert_not_called()
        assert stats["gemini_calls"] == 0
        assert stats["rule_only"] == 1
        assert stats["updated"] == 1

        # The original company/role survive (not clobbered by the blank follow-up).
        drive = db_manager.fetch_opportunity_by_thread_id("thread_known")
        assert drive["company_name"] == "Microsoft"
        assert drive["current_status"] in {"OA", "SHORTLISTED"}

    def test_known_thread_oa_update_still_calls_gemini(
        self, db_manager: DatabaseManager, mock_settings, sample_opportunity
    ):
        """oa_date/interview_date have no rule-based extraction path, so the
        cost guard must not suppress Gemini for OA_UPDATE/INTERVIEW_UPDATE
        follow-ups even on an already-tracked thread."""
        db_manager.insert_or_update_opportunity(
            sample_opportunity("Microsoft", "Software Engineer Intern"),
            source_email_id="orig_msg2",
            source_thread_id="thread_known_oa",
        )

        runner = PlacementTrackerRunner(
            connection=db_manager.connection, settings=mock_settings
        )

        extractor = MagicMock()
        extractor.extract_from_email.return_value = {}

        from placement_mail_tracker.config.user_profile import UserProfile

        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        followup = {
            "message_id": "followup_oa_msg",
            "thread_id": "thread_known_oa",
            "subject": "Online Assessment scheduled - shortlisted candidates",
            "sender": "cdc@college.edu",
            "body_text": "Your online assessment has been scheduled.",
            "timestamp": datetime.now().isoformat(),
        }

        runner._process_single_message(
            followup, db_manager, extractor, UserProfile.load(), stats
        )

        extractor.extract_from_email.assert_called_once()


# ---------------------------------------------------------------------------
# (c) Quota-aware deferral: a genuine daily-quota exhaustion must not be
# silently swallowed into a rule-only "processed" row — it should defer the
# email (PENDING_EXTRACTION) for a later run, without touching retry_count.
# ---------------------------------------------------------------------------


class TestQuotaAwareDeferral:
    def test_quota_exhaustion_defers_instead_of_processing(
        self, db_manager: DatabaseManager, mock_settings
    ):
        runner = PlacementTrackerRunner(
            connection=db_manager.connection, settings=mock_settings
        )

        extractor = MagicMock()
        extractor.extract_from_email.side_effect = GeminiQuotaExhaustedError(
            "429 RESOURCE_EXHAUSTED PerDay"
        )

        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        msg = {
            "message_id": "quota_deferred_msg",
            "thread_id": "thread_quota_new",
            "subject": "OA Scheduled for TestCo Internship 2027",
            "sender": "cdc@college.edu",
            "body_text": (
                "The Online Assessment (OA) for TestCo Internship has been "
                "scheduled on HackerRank."
            ),
            "timestamp": datetime.now().isoformat(),
        }

        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        # Deferred, not processed: no opportunity created from a silent
        # rule-only fallback, and errors (not "processed") is bumped.
        assert stats["processed"] == 0
        assert stats["created"] == 0
        assert stats["errors"] == 1

        row = db_manager.connection.execute(
            "SELECT processed_status, retry_count FROM processed_emails "
            "WHERE gmail_message_id = ?",
            ("quota_deferred_msg",),
        ).fetchone()
        assert row["processed_status"] == "PENDING_EXTRACTION"
        # retry_count must NOT be bumped toward PERMANENT_FAILURE: quota
        # resets on its own schedule and this is not a defect in the email.
        assert row["retry_count"] == 0

        # The retry queue in _fetch_messages re-fetches PENDING_EXTRACTION
        # messages by id — confirm the already_processed guard (which
        # excludes only 'processed'/'skipped'/'PERMANENT_FAILURE') does not
        # treat this row as done, so a later run will pick it back up.
        already_processed = db_manager.connection.execute(
            """
            SELECT id FROM processed_emails
            WHERE gmail_message_id = ?
              AND processed_status IN ('processed', 'skipped', 'PERMANENT_FAILURE')
            """,
            ("quota_deferred_msg",),
        ).fetchone()
        assert already_processed is None

    def test_quota_exhaustion_does_not_dead_letter_across_repeated_runs(
        self, db_manager: DatabaseManager, mock_settings
    ):
        """Unlike a generic extraction error, repeated quota exhaustion never
        trips _RETRY_MAX / PERMANENT_FAILURE — it just keeps deferring."""
        runner = PlacementTrackerRunner(
            connection=db_manager.connection, settings=mock_settings
        )
        extractor = MagicMock()
        extractor.extract_from_email.side_effect = GeminiQuotaExhaustedError("429 PerDay")

        msg = {
            "message_id": "quota_deferred_repeat",
            "thread_id": "thread_quota_repeat",
            "subject": "Interview scheduled for TestCo",
            "sender": "cdc@college.edu",
            "body_text": "Your technical interview round has been scheduled.",
            "timestamp": datetime.now().isoformat(),
        }
        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }

        for _ in range(_RETRY_MAX + 2):
            runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        row = db_manager.connection.execute(
            "SELECT processed_status, retry_count FROM processed_emails WHERE gmail_message_id = ?",
            ("quota_deferred_repeat",),
        ).fetchone()
        assert row["processed_status"] == "PENDING_EXTRACTION"
        assert row["retry_count"] == 0

    def test_get_quota_deferred_count_only_counts_quota_causes(
        self, db_manager: DatabaseManager, mock_settings
    ):
        """Quota-death visibility (docs/design/06-extraction-reliability.md):
        the digest-facing count must isolate quota-caused PENDING_EXTRACTION
        rows from a generic transient-error retry using the same status."""
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
        extractor = MagicMock()

        def _msg(msg_id: str) -> dict:
            return {
                "message_id": msg_id, "thread_id": f"thread_{msg_id}",
                "subject": "Interview scheduled for TestCo", "sender": "cdc@college.edu",
                "body_text": "Your technical interview round has been scheduled.",
                "timestamp": datetime.now().isoformat(),
            }

        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }

        profile = UserProfile.load()
        extractor.extract_from_email.side_effect = GeminiQuotaExhaustedError("429 PerDay")
        runner._process_single_message(_msg("quota_a"), db_manager, extractor, profile, stats)
        runner._process_single_message(_msg("quota_b"), db_manager, extractor, profile, stats)

        extractor.extract_from_email.side_effect = ValueError("transient network blip")
        runner._process_single_message(_msg("transient_c"), db_manager, extractor, profile, stats)

        assert db_manager.get_quota_deferred_count() == 2


# ---------------------------------------------------------------------------
# Data-quality guards surfaced by a live run
# ---------------------------------------------------------------------------


class TestStatusCanonicalization:
    def test_new_and_ppt_map_to_open(self):
        assert canonicalize_status("NEW") == "OPEN"
        assert canonicalize_status("PPT") == "OPEN"
        assert canonicalize_status("applied") == "REGISTERED"

    def test_known_status_passthrough(self):
        assert canonicalize_status("OA") == "OA"
        assert canonicalize_status(None) is None


class TestIdentifiableCompany:
    def test_unknown_variants_are_not_identifiable(self):
        for bad in (None, "", "Unknown", "unknown company", "  UNKNOWN  "):
            assert _is_identifiable_company(bad) is False

    def test_real_company_is_identifiable(self):
        assert _is_identifiable_company("Microsoft") is True


class TestUnidentifiableDriveHandling:
    def test_alert_generator_skips_unknown_company(
        self, db_manager: DatabaseManager, mock_settings
    ):
        # A drive with no real company and a deadline tomorrow.
        tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
        db_manager.insert_or_update_opportunity(
            {
                "company_name": "Unknown",
                "role": "Unknown Role",
                "deadline": tomorrow,
                "current_status": "OPEN",
                "eligibility_status": "ELIGIBLE",
            },
            source_email_id="unknown_alert",
        )
        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()  # never touch real SMTP
        gen.check_and_send_alerts()
        gen.notifier.send_email.assert_not_called()

    def test_runner_does_not_create_drive_without_company(
        self, db_manager: DatabaseManager, mock_settings
    ):
        from placement_mail_tracker.config.user_profile import UserProfile

        runner = PlacementTrackerRunner(
            connection=db_manager.connection, settings=mock_settings
        )
        # Gemini returns a result with no company name.
        extractor = MagicMock()
        extractor.extract_from_email.return_value = {"company_name": None, "role": "X"}

        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
        }
        msg = {
            "message_id": "no_company_msg",
            "thread_id": "brand_new_thread",
            "subject": "Internship registration is now open",
            "sender": "cdc@college.edu",
            "body_text": "Apply via the portal.",
            "timestamp": datetime.now().isoformat(),
        }
        before = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        runner._process_single_message(
            msg, db_manager, extractor, UserProfile.load(), stats
        )

        after = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]
        assert after == before  # no "Unknown" drive created
        assert stats["skipped"] == 1
        assert stats["created"] == 0


@patch("placement_mail_tracker.scheduler.runner.DatabaseManager")
@patch("placement_mail_tracker.scheduler.runner.GmailClient")
@patch("placement_mail_tracker.scheduler.runner.GeminiExtractor")
@patch("placement_mail_tracker.scheduler.runner.SheetsClient")
def test_fetch_window_not_advanced_on_gmail_failure(
    mock_sheets, mock_gemini, mock_gmail, mock_db, runner_settings
):
    original = "2026-06-01T12:00:00Z"
    state_file = Path(runner_settings.fetch_state_file)
    state_file.write_text(
        json.dumps({"last_successful_fetch": original}), encoding="utf-8"
    )

    mock_db.return_value.get_active_opportunities.return_value = []
    # Gmail blows up -> fetch did not succeed.
    mock_gmail.return_value.fetch_recent_messages_since.side_effect = ConnectionError(
        "network down"
    )

    runner = PlacementTrackerRunner(connection=MagicMock(), settings=runner_settings)
    runner.run_once()

    # The window must be left exactly where it was so no mail is skipped.
    new_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert new_state["last_successful_fetch"] == original


@patch("placement_mail_tracker.scheduler.runner.rule_extract")
def test_extraction_failure_reaches_permanent_failure_at_retry_max(
    mock_rule, db_manager, mock_settings
):
    """After _RETRY_MAX consecutive failures, email moves to PERMANENT_FAILURE (FS INV-26)."""
    from placement_mail_tracker.config.user_profile import UserProfile

    mock_rule.side_effect = RuntimeError("extraction always fails")
    runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
    extractor = MagicMock()
    stats = {
        "processed": 0, "skipped": 0, "errors": 0,
        "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
    }
    msg = {
        "message_id": "dead_letter_test",
        "thread_id": "dead_letter_thread",
        "subject": "Internship registration is now open",
        "sender": "cdc@college.edu",
        "body_text": "Apply via the portal.",
        "timestamp": datetime.now().isoformat(),
    }
    for _ in range(_RETRY_MAX):
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

    row = db_manager.connection.execute(
        "SELECT processed_status, retry_count FROM processed_emails WHERE gmail_message_id = ?",
        (msg["message_id"],),
    ).fetchone()
    assert row["processed_status"] == "PERMANENT_FAILURE"
    assert row["retry_count"] == _RETRY_MAX
    assert stats["errors"] == _RETRY_MAX


@patch("placement_mail_tracker.scheduler.runner.DatabaseManager")
@patch("placement_mail_tracker.scheduler.runner.GmailClient")
@patch("placement_mail_tracker.scheduler.runner.GeminiExtractor")
@patch("placement_mail_tracker.scheduler.runner.SheetsClient")
def test_fetch_window_not_advanced_on_suppressed_gmail_error(
    mock_sheets, mock_gemini, mock_gmail, mock_db, runner_settings
):
    # In non-production env the Gmail client swallows HttpError/auth failures
    # and returns [] while recording last_error. That empty list must NOT look
    # like a successful zero-mail fetch, or the window would advance past unread
    # mail (FS INV-7/INV-8).
    original = "2026-06-01T12:00:00Z"
    state_file = Path(runner_settings.fetch_state_file)
    state_file.write_text(
        json.dumps({"last_successful_fetch": original}), encoding="utf-8"
    )

    mock_db.return_value.get_active_opportunities.return_value = []
    gmail_instance = mock_gmail.return_value
    gmail_instance.fetch_recent_messages_since.return_value = []
    # Simulate a suppressed failure: empty result but a real error string.
    gmail_instance.last_error = "HttpError 503: backend error"

    runner = PlacementTrackerRunner(connection=MagicMock(), settings=runner_settings)
    runner.run_once()

    new_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert new_state["last_successful_fetch"] == original
