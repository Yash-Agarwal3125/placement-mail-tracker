"""docs/design/16 Phase B: wiring roster verification into the mail pipeline.

Mocks ``_prepare_attachments`` (real xlsx/pdf byte fixtures aren't needed --
that extraction path is already covered by ai/attachments.py's own tests)
to exercise the classification -> event_type -> verify_roster -> DB write
chain end-to-end through ``_process_single_message``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner


def _mock_settings(tmp_path) -> Settings:
    return Settings(
        gemini_api_key="fake",
        google_sheet_id="fake_sheet",
        notification_email="fake@example.com",
        gmail_client_id="fake",
        gmail_client_secret="fake",
        telegram_bot_token="fake-token",
        telegram_chat_id="fake-chat-id",
        smtp_email="test@example.com",
        smtp_app_password="fake-password",
        email_receiver="receiver@example.com",
        app_env="testing",
        fetch_state_file=str(tmp_path / "fetch_state.json"),
    )


def _profile(**overrides) -> UserProfile:
    base = dict(
        degree="B.Tech", branch="Computer Science", campus="Vellore",
        graduation_year=2027, cgpa=8.0,
        full_name="Yash Agarwal", registration_no="23BAI0011", codenames=["X2A6L2T3"],
    )
    base.update(overrides)
    return UserProfile(**base)


def _stats() -> dict:
    return {
        "processed": 0, "skipped": 0, "errors": 0,
        "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0,
    }


def test_oa_update_with_matching_roster_writes_matched_verdict(
    db_manager: DatabaseManager, tmp_path, sample_opportunity
):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"),
        source_email_id="orig", source_thread_id="thread_oa",
    )
    runner = PlacementTrackerRunner(
        connection=db_manager.connection, settings=_mock_settings(tmp_path)
    )
    extractor = MagicMock()

    msg = {
        "message_id": "oa_msg",
        "thread_id": "thread_oa",
        "subject": "Blackrock PPT online test is scheduled on 10-08-2026",
        "sender": "cdc@college.edu",
        "body_text": "OA scheduled for shortlisted students.",
        "timestamp": datetime.now().isoformat(),
        "attachments": [{"filename": "roster.xlsx", "mimeType": "xlsx", "attachmentId": "a1"}],
    }

    matched_roster = ("1. Yash Agarwal 23BAI0011", [])
    with patch.object(PlacementTrackerRunner, "_prepare_attachments", return_value=matched_roster):
        runner._process_single_message(
            msg, db_manager, extractor, _profile(), _stats(), gmail_client=MagicMock()
        )

    verdicts = db_manager.fetch_roster_verdicts()
    assert verdicts[(opp_id, "OA")]["verdict"] == "MATCHED"
    assert verdicts[(opp_id, "OA")]["method"] == "registration_no"


def test_interview_update_with_absent_name_writes_not_matched(
    db_manager: DatabaseManager, tmp_path, sample_opportunity
):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Amazon", "SDE Intern"),
        source_email_id="orig2", source_thread_id="thread_int",
    )
    runner = PlacementTrackerRunner(
        connection=db_manager.connection, settings=_mock_settings(tmp_path)
    )
    extractor = MagicMock()

    msg = {
        "message_id": "int_msg",
        "thread_id": "thread_int",
        "subject": "Amazon technical interview round is scheduled on 10-08-2026",
        "sender": "cdc@college.edu",
        "body_text": "Please be present with your ID card.",
        "timestamp": datetime.now().isoformat(),
        "attachments": [{"filename": "roster.xlsx", "mimeType": "xlsx", "attachmentId": "a1"}],
    }

    roster = "1. John Doe 23BAI0099\n2. Jane Roe 23BAI0100"
    with patch.object(PlacementTrackerRunner, "_prepare_attachments", return_value=(roster, [])):
        runner._process_single_message(
            msg, db_manager, extractor, _profile(), _stats(), gmail_client=MagicMock()
        )

    verdicts = db_manager.fetch_roster_verdicts()
    assert verdicts[(opp_id, "INTERVIEW")]["verdict"] == "NOT_MATCHED"


def test_unverified_identity_never_writes_a_verdict(
    db_manager: DatabaseManager, tmp_path, sample_opportunity
):
    db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"),
        source_email_id="orig3", source_thread_id="thread_unverified",
    )
    runner = PlacementTrackerRunner(
        connection=db_manager.connection, settings=_mock_settings(tmp_path)
    )
    extractor = MagicMock()

    msg = {
        "message_id": "unverified_msg",
        "thread_id": "thread_unverified",
        "subject": "Blackrock OA scheduled",
        "sender": "cdc@college.edu",
        "body_text": "OA scheduled.",
        "timestamp": datetime.now().isoformat(),
        "attachments": [{"filename": "roster.xlsx", "mimeType": "xlsx", "attachmentId": "a1"}],
    }

    stub = ("anything", [])
    with patch.object(PlacementTrackerRunner, "_prepare_attachments", return_value=stub):
        runner._process_single_message(
            msg, db_manager, extractor, UserProfile._default(), _stats(), gmail_client=MagicMock()
        )

    assert db_manager.fetch_roster_verdicts() == {}


def test_shortlist_update_classification_never_writes_a_verdict(
    db_manager: DatabaseManager, tmp_path, sample_opportunity
):
    """SHORTLIST_UPDATE is deliberately excluded -- ambiguous which round."""
    db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"),
        source_email_id="orig4", source_thread_id="thread_shortlist",
    )
    runner = PlacementTrackerRunner(
        connection=db_manager.connection, settings=_mock_settings(tmp_path)
    )
    extractor = MagicMock()

    msg = {
        "message_id": "shortlist_msg",
        "thread_id": "thread_shortlist",
        "subject": "Blackrock shortlisted candidates for next round",
        "sender": "cdc@college.edu",
        "body_text": "Shortlisted for next round.",
        "timestamp": datetime.now().isoformat(),
        "attachments": [{"filename": "roster.xlsx", "mimeType": "xlsx", "attachmentId": "a1"}],
    }

    matched_roster = ("1. Yash Agarwal 23BAI0011", [])
    with patch.object(PlacementTrackerRunner, "_prepare_attachments", return_value=matched_roster):
        runner._process_single_message(
            msg, db_manager, extractor, _profile(), _stats(), gmail_client=MagicMock()
        )

    assert db_manager.fetch_roster_verdicts() == {}


def test_no_attachments_never_writes_a_verdict(
    db_manager: DatabaseManager, tmp_path, sample_opportunity
):
    db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"),
        source_email_id="orig5", source_thread_id="thread_noattach",
    )
    runner = PlacementTrackerRunner(
        connection=db_manager.connection, settings=_mock_settings(tmp_path)
    )
    extractor = MagicMock()

    msg = {
        "message_id": "noattach_msg",
        "thread_id": "thread_noattach",
        "subject": "Blackrock OA scheduled",
        "sender": "cdc@college.edu",
        "body_text": "OA scheduled.",
        "timestamp": datetime.now().isoformat(),
        "attachments": [],
    }

    runner._process_single_message(
        msg, db_manager, extractor, _profile(), _stats(), gmail_client=MagicMock()
    )

    assert db_manager.fetch_roster_verdicts() == {}
