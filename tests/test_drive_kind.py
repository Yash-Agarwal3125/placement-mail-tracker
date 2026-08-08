"""docs/design/16 Phase 2: distinguishing placement drives from non-drive mail.

The calendar-relevance investigation found real mail marked ELIGIBLE that was
never a placement drive at all (hackathons, scholarships, workshops, research
postings), each pulled from live production data.
"""

from __future__ import annotations

from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.rule_engine import classify_drive_kind, extract_from_email
from scripts.backfill_drive_kind import backfill


def test_hackathon_subject():
    subject = "Adobe University Hackathon 2026 - Registration Open"
    assert classify_drive_kind(subject) == "HACKATHON"


def test_innovation_challenge_is_hackathon():
    subject = "Suzuki R&D Center | Igniters Innovation Challenge 2026 is LIVE"
    assert classify_drive_kind(subject) == "HACKATHON"


def test_innohack_without_word_boundary_is_hackathon():
    subject = "Invitation to Participate in InnoHack 2.0 - Two-Day Hackathon"
    assert classify_drive_kind(subject) == "HACKATHON"


def test_scholarship_subject():
    subject = "NSP (National Scholarship Portal) Fresh and Renewal Application"
    assert classify_drive_kind(subject) == "SCHOLARSHIP"


def test_government_scholarship_scheme():
    assert classify_drive_kind("Government of Tamil Nadu's Scholarship Scheme") == "SCHOLARSHIP"


def test_phd_postdoc_is_research():
    subject = "France Research Opportunities | 4 New PhD & Post-doc Opportunities"
    assert classify_drive_kind(subject) == "RESEARCH"


def test_workshop_subject():
    subject = "Two-Day Workshop on Build Your VCU Prototype in One Day"
    assert classify_drive_kind(subject) == "WORKSHOP"


def test_real_drive_is_placement():
    subject = "Confirmed: Your Registration for BlackRock Placement Drive"
    assert classify_drive_kind(subject) == "PLACEMENT"
    assert classify_drive_kind("Amazon PPT & online test is scheduled on 10-08-2026") == "PLACEMENT"


def test_placement_process_mail_overrides_keyword_collision():
    """Real production case (opp 108, Honeywell): a stray/misattributed
    "...Campus Connect Hackathon..." mail landed on the same drive row as a
    genuine OFFER_UPDATE mail (a separate company-fuzzy-match issue,
    docs/design/15 §1). The offer mail's classification must win -- a
    keyword collision on unrelated mail must never hide a real drive's
    events from the calendar.
    """
    subject = "Congratulations!! Honeywell Super Dream Internship Selection List 2027 batch !!"
    assert classify_drive_kind(subject, "", "OFFER_UPDATE") == "PLACEMENT"
    assert classify_drive_kind(
        "Honeywell PPT online test is scheduled on 05-08-2026", "", "OA_UPDATE"
    ) == "PLACEMENT"


def test_extract_from_email_sets_drive_kind():
    result = extract_from_email("Odoo x NMIT Bangalore Hackathon 2026", "")
    assert result.drive_kind == "HACKATHON"
    assert result.to_dict()["drive_kind"] == "HACKATHON"

    result = extract_from_email("Flipkart Super Dream Internship Registration - 2027 Batch", "")
    assert result.drive_kind == "PLACEMENT"


def test_insert_defaults_to_placement(db_manager: DatabaseManager, sample_opportunity):
    opp = sample_opportunity("Google", "SDE Intern")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="g1")
    record = db_manager.fetch_opportunity_by_id(opp_id)
    assert record["drive_kind"] == "PLACEMENT"


def test_insert_records_hackathon(db_manager: DatabaseManager, sample_opportunity):
    opp = {**sample_opportunity("Adobe", "Hackathon Participant"), "drive_kind": "HACKATHON"}
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="a1")
    record = db_manager.fetch_opportunity_by_id(opp_id)
    assert record["drive_kind"] == "HACKATHON"


def test_followup_without_keyword_does_not_downgrade_hackathon(
    db_manager: DatabaseManager, sample_opportunity
):
    """A reminder mail that doesn't repeat 'hackathon' must not un-flag the drive."""
    opp = {**sample_opportunity("Adobe", "Hackathon Participant"), "drive_kind": "HACKATHON"}
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="a1")

    followup = {
        **sample_opportunity("Adobe", "Hackathon Participant"),
        "current_status": "REGISTERED",
    }
    opp_id2, created = db_manager.insert_or_update_opportunity(followup, source_email_id="a2")
    assert opp_id == opp_id2
    assert created is False

    record = db_manager.fetch_opportunity_by_id(opp_id)
    assert record["drive_kind"] == "HACKATHON"


def test_backfill_reclassifies_from_stored_subjects(
    db_manager: DatabaseManager, sample_opportunity
):
    """docs/design/16 Phase 2 backfill: rows that predate drive_kind (still
    PLACEMENT from the column default) get reclassified from the subjects
    already stored in processed_emails, without touching real drives."""
    hackathon_opp = sample_opportunity("Adobe", "Hackathon Participant")
    hackathon_id, _ = db_manager.insert_or_update_opportunity(hackathon_opp, source_email_id="a1")
    db_manager.log_processed_email(
        gmail_message_id="a1",
        subject="Adobe University Hackathon 2026 - Registration Open",
        sender="cdc@vit.ac.in",
        received_at="2026-07-01T00:00:00",
        opportunity_id=hackathon_id,
        processed_status="processed",
    )

    real_opp = sample_opportunity("Blackrock", "SDE Intern")
    real_id, _ = db_manager.insert_or_update_opportunity(real_opp, source_email_id="b1")
    db_manager.log_processed_email(
        gmail_message_id="b1",
        subject="Confirmed: Your Registration for BlackRock Placement Drive",
        sender="cdc@vit.ac.in",
        received_at="2026-07-01T00:00:00",
        opportunity_id=real_id,
        processed_status="processed",
        email_classification="APPLICATION_CONFIRMATION",
    )

    stats = backfill(db_manager, dry_run=False)
    assert stats["reclassified"] == 1

    assert db_manager.fetch_opportunity_by_id(hackathon_id)["drive_kind"] == "HACKATHON"
    assert db_manager.fetch_opportunity_by_id(real_id)["drive_kind"] == "PLACEMENT"


def test_backfill_dry_run_writes_nothing(db_manager: DatabaseManager, sample_opportunity):
    opp = sample_opportunity("Adobe", "Hackathon Participant")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="a1")
    db_manager.log_processed_email(
        gmail_message_id="a1",
        subject="Adobe University Hackathon 2026",
        sender="cdc@vit.ac.in",
        received_at="2026-07-01T00:00:00",
        opportunity_id=opp_id,
        processed_status="processed",
    )
    stats = backfill(db_manager, dry_run=True)
    assert stats["reclassified"] == 1
    assert db_manager.fetch_opportunity_by_id(opp_id)["drive_kind"] == "PLACEMENT"


def test_backfill_placement_process_mail_beats_earlier_stray_keyword(
    db_manager: DatabaseManager, sample_opportunity
):
    """Real production case (opp 108, Honeywell): an earlier stray/
    misattributed hackathon mail on the same drive row must not outrank a
    later genuine OFFER_UPDATE mail proving it's a real placement drive."""
    opp = sample_opportunity("Honeywell", "SDE Intern")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="h1")
    db_manager.log_processed_email(
        gmail_message_id="h1",
        subject="Honeywell Technologies Campus Connect Hackathon - Register now",
        sender="cdc@vit.ac.in",
        received_at="2026-07-15T00:00:00",
        opportunity_id=opp_id,
        processed_status="processed",
        email_classification="IRRELEVANT",
    )
    db_manager.log_processed_email(
        gmail_message_id="h2",
        subject="Congratulations!! Honeywell Selection List 2027 batch",
        sender="cdc@vit.ac.in",
        received_at="2026-08-01T00:00:00",
        opportunity_id=opp_id,
        processed_status="processed",
        email_classification="OFFER_UPDATE",
    )
    stats = backfill(db_manager, dry_run=False)
    assert stats["reclassified"] == 0
    assert db_manager.fetch_opportunity_by_id(opp_id)["drive_kind"] == "PLACEMENT"
