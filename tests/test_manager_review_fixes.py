"""Tests for docs/design/09-manager-review.md's Top-5 fixes: F1 (rescue
unattributed follow-ups), F2 (dual-date event alerts) + the deadline-
escalation backlog item, F3 (self-mail short-circuit), and the dead
fallback-model-list refresh.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.rule_engine import RuleExtractionResult
from placement_mail_tracker.scheduler import runner as runner_module
from placement_mail_tracker.scheduler.alert_generator import AlertGenerator
from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner
from placement_mail_tracker.scheduler.unattributed_mail_store import (
    pop_pending_unattributed_mail,
)


def _stats() -> dict[str, int]:
    return {
        "processed": 0, "skipped": 0, "errors": 0,
        "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
    }


# ---------------------------------------------------------------------------
# F1: rescue unattributed follow-ups at the "no identifiable company" gate
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_unattributed_mail_file(tmp_path, monkeypatch):
    from placement_mail_tracker.scheduler import unattributed_mail_store

    monkeypatch.setattr(
        unattributed_mail_store, "_FLAGS_FILE", tmp_path / "unattributed_mail.json"
    )
    monkeypatch.setattr(
        runner_module, "append_unattributed_mail", unattributed_mail_store.append_unattributed_mail
    )


class TestF1RescueUnattributedFollowups:
    """The three real subjects quoted verbatim in doc 09's F1: extraction
    whiffs on the company name, but it's the first word of the subject and an
    active drive already carries it — the mail must attach to that drive
    instead of being dropped."""

    def _run_rescue_case(self, db_manager, mock_settings, seed_company, subject):
        db_manager.insert_or_update_opportunity(
            {"company_name": seed_company, "role": "SDE", "current_status": "OPEN"},
            source_email_id=f"seed_{seed_company}",
        )
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
        extractor = MagicMock()
        extractor.extract_from_email.return_value = {
            "company_name": None,
            "role": None,
            "oa_date": "2026-07-10T13:30:00",
        }
        stats = _stats()
        msg = {
            "message_id": f"msg_{seed_company}",
            "thread_id": f"new_thread_{seed_company}",
            "subject": subject,
            "sender": "cdc@college.edu",
            "body_text": "Please be ready and log in on time.",
            "timestamp": datetime.now().isoformat(),
        }
        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        with patch.object(
            runner_module,
            "rule_extract",
            return_value=RuleExtractionResult(
                company_name=None,
                role=None,
                current_status="OPEN",
                email_classification="OA_UPDATE",
            ),
        ):
            runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        return stats, before, after

    def test_resmed_subject_rescues_to_existing_drive(self, db_manager, mock_settings):
        stats, before, after = self._run_rescue_case(
            db_manager, mock_settings, "Resmed",
            "Resmed online test is scheduled on 10th July 2026 by 1.30 pm PRP 717",
        )
        assert after == before, "no new drive should be created for a rescued mail"
        assert stats["created"] == 0
        assert stats["skipped"] == 0

    def test_varroc_subject_rescues_to_existing_drive(self, db_manager, mock_settings):
        stats, before, after = self._run_rescue_case(
            db_manager, mock_settings, "Varroc",
            "Varroc next round of selection process is scheduled on 13th July 2026 by 08:30 am",
        )
        assert after == before
        assert stats["created"] == 0
        assert stats["skipped"] == 0

    def test_valuelabs_subject_rescues_to_existing_drive(self, db_manager, mock_settings):
        stats, before, after = self._run_rescue_case(
            db_manager, mock_settings, "Valuelabs",
            "Valuelabs online test, PPT and selection is scheduled on 16th and 17th July",
        )
        assert after == before
        assert stats["created"] == 0
        assert stats["skipped"] == 0

    def test_unmatchable_date_bearing_mail_is_skipped_and_flagged_in_digest(
        self, db_manager, mock_settings
    ):
        """No active drive matches -> still skipped (unattributable), but the
        digest gets a line instead of the mail vanishing silently."""
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
        extractor = MagicMock()
        extractor.extract_from_email.return_value = {
            "company_name": None,
            "role": None,
            "oa_date": "2026-07-20T09:00:00",
        }
        stats = _stats()
        subject = "TotallyNovelCorp online test is scheduled on 20th July 2026"
        msg = {
            "message_id": "msg_novel",
            "thread_id": "new_thread_novel",
            "subject": subject,
            "sender": "cdc@college.edu",
            "body_text": "Please be ready and log in on time.",
            "timestamp": datetime.now().isoformat(),
        }
        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        with patch.object(
            runner_module,
            "rule_extract",
            return_value=RuleExtractionResult(
                company_name=None,
                role=None,
                current_status="OPEN",
                email_classification="OA_UPDATE",
            ),
        ):
            runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert after == before
        assert stats["skipped"] == 1
        assert stats["created"] == 0

        pending = pop_pending_unattributed_mail()
        assert any(subject in line for line in pending)


# ---------------------------------------------------------------------------
# F2: event alerts must fire on oa_date/interview_date directly
# ---------------------------------------------------------------------------


class TestF2DualDateEventAlerts:
    def test_varroc_shape_oa_past_interview_future_fires_interview_alert(
        self, db_manager: DatabaseManager, mock_settings
    ):
        """docs/design/09 F2's exact live shape: OA already passed, interview
        is within the 24h window -> must fire an interview alert even though
        the drive's single next_event_date would have gone stale on the OA."""
        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()

        now = datetime(2026, 7, 13, 6, 0, 0)
        opp = {
            "id": 42,
            "company_name": "Varroc",
            "eligibility_status": "ELIGIBLE",
            "my_status": "REGISTERED",
            "oa_date": (now - timedelta(days=5)).isoformat(),
            "interview_date": (now + timedelta(hours=20)).isoformat(),
        }

        gen._check_event_alerts(opp, now)

        gen.notifier.send_email.assert_called_once()
        subject = gen.notifier.send_email.call_args[1]["subject"]
        assert "INTERVIEW" in subject
        assert "Varroc" in subject

    def test_second_event_alert_is_independent_of_first(
        self, db_manager: DatabaseManager, mock_settings
    ):
        """Both oa_date and interview_date can each independently fire their
        own alert within the same run when both are upcoming."""
        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()

        now = datetime(2026, 7, 8, 6, 0, 0)
        opp = {
            "id": 43,
            "company_name": "Groww",
            "eligibility_status": "ELIGIBLE",
            "my_status": "REGISTERED",
            "oa_date": (now + timedelta(hours=20)).isoformat(),
            "interview_date": (now + timedelta(hours=40)).isoformat(),
        }

        gen._check_event_alerts(opp, now)

        assert gen.notifier.send_email.call_count == 2


# ---------------------------------------------------------------------------
# Backlog item 1: unapplied-deadline escalation
# ---------------------------------------------------------------------------


# NOTE: the deadline-escalation tests that used to live here were rewritten
# for the batched T48/T24 design locked in docs/design/10-confirmation-and-
# reminders.md Feature 2 -- see tests/test_reminder_escalation.py.


# ---------------------------------------------------------------------------
# F3: self-generated tracker/CI mail never reaches extraction
# ---------------------------------------------------------------------------


class TestF3SelfGeneratedMailShortCircuit:
    def _process(self, db_manager, mock_settings, subject, sender):
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
        extractor = MagicMock()
        extractor.extract_from_email.side_effect = AssertionError("Gemini was called!")
        stats = _stats()
        msg = {
            "message_id": f"self_{abs(hash((subject, sender)))}",
            "thread_id": "self_thread",
            "subject": subject,
            "sender": sender,
            "body_text": "irrelevant body",
            "timestamp": datetime.now().isoformat(),
        }
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)
        extractor.extract_from_email.assert_not_called()
        return stats, msg["message_id"]

    def test_own_failure_streak_alert_is_short_circuited(self, db_manager, mock_settings):
        stats, msg_id = self._process(
            db_manager, mock_settings,
            "Placement Mail Tracker failure streak: 3",
            mock_settings.smtp_email,
        )
        assert stats["skipped"] == 1
        row = db_manager.connection.execute(
            "SELECT processed_status FROM processed_emails WHERE gmail_message_id = ?",
            (msg_id,),
        ).fetchone()
        assert row["processed_status"] == "SELF_NOISE"

    def test_own_upcoming_event_alert_is_short_circuited(self, db_manager, mock_settings):
        self._process(
            db_manager, mock_settings,
            "\U0001F4C5 UPCOMING EVENT: Resmed in <23 hours",
            mock_settings.smtp_email,
        )

    def test_github_ci_mail_is_short_circuited(self, db_manager, mock_settings):
        self._process(
            db_manager, mock_settings,
            "[Yash-Agarwal3125/placement-mail-tracker] Run failed: CI - main",
            "notifications@github.com",
        )

    def test_normal_cdc_mail_is_untouched(self, db_manager, mock_settings):
        runner = PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)
        extractor = MagicMock()
        extractor.extract_from_email.return_value = {
            "company_name": "Infosys", "role": "SDE",
        }
        stats = _stats()
        msg = {
            "message_id": "normal_cdc_mail",
            "thread_id": "normal_thread",
            "subject": "Infosys registration is now open",
            "sender": "cdc@college.edu",
            "body_text": "Apply via the portal.",
            "timestamp": datetime.now().isoformat(),
        }
        runner._process_single_message(msg, db_manager, extractor, UserProfile.load(), stats)

        row = db_manager.connection.execute(
            "SELECT processed_status FROM processed_emails WHERE gmail_message_id = ?",
            ("normal_cdc_mail",),
        ).fetchone()
        assert row["processed_status"] != "SELF_NOISE"


# ---------------------------------------------------------------------------
# Gemini fallback model-list refresh
# ---------------------------------------------------------------------------


class TestGeminiFallbackModelListRefresh:
    def test_no_dead_or_retired_models_remain(self):
        dead_or_retired = {
            "gemini-2.5-flash-lite-preview-06-17",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
        }
        settings = Settings(GEMINI_API_KEY="fake")
        assert not (set(settings.gemini_fallback_models) & dead_or_retired)
        assert len(settings.gemini_fallback_models) == 1
