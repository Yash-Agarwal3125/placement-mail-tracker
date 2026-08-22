"""Runner-level integration tests for Feature 1 (docs/design/10-confirmation-
and-reminders.md): the D4 ladder, D1 sender-gated classification, D2 filter
allow-rule, and D3 (my_status ONLY, never creates a drive or touches
current_status) all meeting in the actual _process_single_message path.

All subject/body fixtures are SYNTHETIC (docs/design/08-confirmation-audit.md
found zero real samples).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.extraction.confirmation import CONFIRMATION_SENDER
from placement_mail_tracker.gmail.filters import calculate_relevance_score
from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner


def _stats() -> dict[str, int]:
    return {
        "processed": 0, "skipped": 0, "errors": 0,
        "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
    }


@pytest.fixture(autouse=True)
def _isolate_corpus_capture(tmp_path, monkeypatch):
    from placement_mail_tracker.scheduler import confirmation_corpus

    monkeypatch.setattr(confirmation_corpus, "CORPUS_DIR", tmp_path / "confirmations")
    monkeypatch.setattr(confirmation_corpus, "LABELS_FILE", tmp_path / "labels.csv")


def _confirmation_msg(msg_id: str, subject: str, body: str) -> dict:
    return {
        "message_id": msg_id,
        "thread_id": f"thread_{msg_id}",
        "subject": subject,
        "sender": CONFIRMATION_SENDER,
        "body_text": body,
        "timestamp": datetime.now().isoformat(),
    }


class TestConfirmationRunnerIntegration:
    def test_observe_mode_never_writes_status(self, db_manager, mock_settings, sample_opportunity):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Resmed", "SDE Intern"), source_email_id="seed_resmed",
        )
        settings = mock_settings.model_copy(update={"confirmation_mode": "observe"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()
        extractor.extract_from_email.side_effect = AssertionError("Gemini must not be called")

        msg = _confirmation_msg(
            "conf_1", "Application Confirmation",
            "You have successfully applied for Resmed Software Engineer Intern.",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        row = db_manager.fetch_opportunity_by_id(opp_id)
        assert row["my_status"] == "NOT_APPLIED"
        assert stats["created"] == 0
        assert stats["processed"] == 1

    def test_enforce_mode_confirmed_tier_writes_applied(
        self, db_manager, mock_settings, sample_opportunity
    ):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Varroc", "FTE"), source_email_id="seed_varroc",
        )
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        msg = _confirmation_msg(
            "conf_2", "Confirmation",
            "Your registration has been received for Varroc Full Time role.",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        row = db_manager.fetch_opportunity_by_id(opp_id)
        assert row["my_status"] == "APPLIED"

    def test_unknown_tier_never_writes_even_in_enforce_mode(
        self, db_manager, mock_settings, sample_opportunity
    ):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Valuelabs", "Intern"), source_email_id="seed_valuelabs",
        )
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        msg = _confirmation_msg(
            "conf_3", "Valuelabs Notice",
            "Please check the portal for the latest status regarding Valuelabs.",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        row = db_manager.fetch_opportunity_by_id(opp_id)
        assert row["my_status"] == "NOT_APPLIED"

    def test_no_confident_match_is_persisted_and_surfaced(self, db_manager, mock_settings):
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        msg = _confirmation_msg(
            "conf_4", "Application Confirmation", "Your application has been received.",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        unmatched = db_manager.fetch_unmatched_confirmations()
        assert len(unmatched) == 1
        assert unmatched[0]["gmail_message_id"] == "conf_4"

    def test_duplicate_confirmation_is_a_noop(self, db_manager, mock_settings, sample_opportunity):
        """D6: a second, differently-ID'd confirmation for the same drive
        (message-ID dedup can't catch this) is a no-op via ladder idempotency."""
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Infosys", "SDE"), source_email_id="seed_infosys",
        )
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        body = "You have successfully applied for Infosys SDE role."
        stats = _stats()
        runner._process_single_message(
            _confirmation_msg("conf_5a", "Confirmation", body),
            db_manager, extractor, UserProfile.load(), stats,
        )
        runner._process_single_message(
            _confirmation_msg("conf_5b", "Confirmation", body),
            db_manager, extractor, UserProfile.load(), stats,
        )

        row = db_manager.fetch_opportunity_by_id(opp_id)
        assert row["my_status"] == "APPLIED"

    def test_shortlisted_drive_plus_late_confirmation_is_a_noop(
        self, db_manager, mock_settings, sample_opportunity
    ):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Cisco", "SDE"), source_email_id="seed_cisco",
        )
        drive_id = db_manager.fetch_opportunity_by_id(opp_id)["drive_id"]
        db_manager.set_my_status(drive_id, "SHORTLISTED", source="sheet")

        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        msg = _confirmation_msg(
            "conf_6", "Confirmation", "You have successfully applied for Cisco SDE role.",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        row = db_manager.fetch_opportunity_by_id(opp_id)
        assert row["my_status"] == "SHORTLISTED"

    def test_confirmation_never_creates_a_drive_or_touches_current_status(
        self, db_manager, mock_settings, sample_opportunity
    ):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Groww", "SDE", current_status="OA"), source_email_id="seed_groww",
        )
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()
        extractor.extract_from_email.side_effect = AssertionError("Gemini must not be called")

        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        msg = _confirmation_msg(
            "conf_7", "Confirmation", "You have successfully applied for Groww SDE role.",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)
        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        assert after == before
        row = db_manager.fetch_opportunity_by_id(opp_id)
        assert row["current_status"] == "OA"
        assert row["my_status"] == "APPLIED"

    def test_sender_gate_avoids_offer_update_collision(self, db_manager, mock_settings):
        """D1: 'congratulations' phrasing from the CDC confirmation sender
        must classify as APPLICATION_CONFIRMATION, never OFFER_UPDATE, and
        must never create a spurious drive."""
        settings = mock_settings.model_copy(update={"confirmation_mode": "observe"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()
        extractor.extract_from_email.side_effect = AssertionError("Gemini must not be called")

        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        msg = _confirmation_msg(
            "conf_8", "Congratulations",
            "Congratulations, your application has been submitted successfully.",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)
        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        assert after == before
        row = db_manager.connection.execute(
            "SELECT email_classification FROM processed_emails WHERE gmail_message_id = ?",
            ("conf_8",),
        ).fetchone()
        assert row["email_classification"] == "APPLICATION_CONFIRMATION"


class TestPhase6SubjectResolvedConfirmations:
    """Phase 6 (calendar-drift remediation plan, Cause 7): when the body-
    fuzzy/reference-id match finds nothing, extract the company from the
    real, formulaic VIT CDC confirmation subject and resolve it through the
    same order Phase 1 built (exact hash -> thread_id -> single active
    same-company candidate for the year -> compatible program name), MAY
    create a new drive when none matches at all (unlike a stage-update
    mail), and sets my_status = APPLIED."""

    def test_creates_a_new_drive_when_none_matches(self, db_manager, mock_settings):
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()
        extractor.extract_from_email.side_effect = AssertionError("Gemini must not be called")

        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        msg = _confirmation_msg(
            "conf_p6_1",
            "Congratulations! You're Eligible for Honeywell Aerospace Placement Drive",
            "",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)
        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        assert after == before + 1
        row = db_manager.connection.execute(
            "SELECT * FROM opportunities WHERE company_name = 'Honeywell Aerospace';"
        ).fetchone()
        assert row is not None
        assert row["my_status"] == "APPLIED"
        assert row["email_classification"] == "APPLICATION_CONFIRMATION"

    def test_observe_mode_creates_drive_but_never_writes_status(self, db_manager, mock_settings):
        settings = mock_settings.model_copy(update={"confirmation_mode": "observe"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        msg = _confirmation_msg(
            "conf_p6_2", "Confirmed: Your Registration for Accenture Placement Drive", "",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        row = db_manager.connection.execute(
            "SELECT * FROM opportunities WHERE company_name = 'Accenture';"
        ).fetchone()
        assert row is not None
        assert row["my_status"] == "NOT_APPLIED"

    def test_attaches_to_existing_single_active_candidate_not_creates(
        self, db_manager, mock_settings, sample_opportunity
    ):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Honeywell", "Aerospace Placement Drive"),
            source_email_id="seed_honeywell_aero",
        )
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        msg = _confirmation_msg(
            "conf_p6_3", "Important: Date Change for Honeywell Placement Drive", "",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)
        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        assert after == before
        row = db_manager.fetch_opportunity_by_id(opp_id)
        assert row["my_status"] == "APPLIED"

    def test_two_active_candidates_routes_to_unmatched_confirmations(
        self, db_manager, mock_settings, sample_opportunity
    ):
        id_a, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Honeywell", "Super Dream Internship"),
            source_email_id="seed_honeywell_a",
        )
        id_b, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Honeywell", "Aerospace Placement Drive"),
            source_email_id="seed_honeywell_b",
        )
        assert id_a != id_b
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        msg = _confirmation_msg(
            "conf_p6_4", "Confirmed: Your Registration for Honeywell", "",
        )
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)
        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        assert after == before  # attached to neither, created nothing
        assert db_manager.fetch_opportunity_by_id(id_a)["my_status"] == "NOT_APPLIED"
        assert db_manager.fetch_opportunity_by_id(id_b)["my_status"] == "NOT_APPLIED"
        unmatched = db_manager.fetch_unmatched_confirmations()
        assert len(unmatched) == 1

    def test_unrecognized_subject_still_routes_to_unmatched_confirmations(
        self, db_manager, mock_settings
    ):
        """A mail with no fuzzy body match AND no recognized subject phrase
        must fall back to the pre-Phase-6 behaviour, not create a drive."""
        settings = mock_settings.model_copy(update={"confirmation_mode": "enforce"})
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
        extractor = MagicMock()

        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        msg = _confirmation_msg("conf_p6_5", "Application Confirmation", "")
        stats = _stats()
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)
        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        assert after == before
        unmatched = db_manager.fetch_unmatched_confirmations()
        assert len(unmatched) == 1
        assert unmatched[0]["gmail_message_id"] == "conf_p6_5"


class TestConfirmationSenderFilterAllowRule:
    """D2: an explicit allow rule, not reliance on the relaxed/trusted paths."""

    def test_confirmation_sender_always_passes_regardless_of_body(self):
        decision = calculate_relevance_score(
            subject="Some ambiguous subject",
            sender=CONFIRMATION_SENDER,
            body="unsubscribe newsletter discount",  # would normally trip negative keywords
        )
        assert decision.is_placement is True
        assert "explicit_allow:cdc_confirmation" in decision.matched_sender_terms

    def test_unrelated_sender_is_unaffected(self):
        decision = calculate_relevance_score(
            subject="Unrelated", sender="someone@example.com", body="hello",
        )
        assert "explicit_allow:cdc_confirmation" not in decision.matched_sender_terms
