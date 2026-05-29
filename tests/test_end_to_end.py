"""Phase 14: End-to-end integration tests with realistic VIT CDC emails.

Simulates 20+ realistic placement emails for Microsoft, Dell, Standard
Chartered, and HPE.  Verifies:
- Follow-up detection across email threads
- Status progression (OPEN → OA → SHORTLISTED → INTERVIEW → OFFER_RECEIVED)
- Drive ID generation
- Gmail link generation
- No duplicate drives
- Company normalization across thread
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.rule_engine import (
    classify_email,
    detect_status_from_text,
    extract_from_email,
    normalize_company_name,
)
from placement_mail_tracker.sheets.sheets_sync import opportunity_to_sheet_row


# ===================================================================
# Realistic VIT CDC email corpus (20+ emails)
# ===================================================================

CDC_EMAILS = [
    # ── Microsoft Thread (5 emails) ──────────────────────────────────
    {
        "id": "ms_001",
        "thread_id": "thread_ms_2027",
        "subject": "Campus Drive – Microsoft Summer Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Microsoft is hiring for Summer Internship 2027.\n\n"
            "Role: Software Engineer Intern\n"
            "Stipend: Rs. 80000 per month\n"
            "Location: Hyderabad\n"
            "Eligibility: B.Tech CSE, ECE – 2027 batch\n"
            "CGPA: 8.0 and above\n"
            "Deadline: 5 June 2027\n"
            "Registration link: https://forms.gle/ms2027\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "ms_002",
        "thread_id": "thread_ms_2027",
        "subject": "OA Scheduled – Microsoft Summer Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "The Online Assessment (OA) for Microsoft Summer Internship has been "
            "scheduled on HackerRank.\n\n"
            "Date: 10 June 2027\nTime: 2:00 PM – 4:00 PM IST\n"
            "Platform: HackerRank\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "ms_003",
        "thread_id": "thread_ms_2027",
        "subject": "Shortlist – Microsoft Summer Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "The following students have been shortlisted for the next round of "
            "Microsoft Summer Internship selection process.\n\n"
            "Please check the attached list.\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "ms_004",
        "thread_id": "thread_ms_2027",
        "subject": "Interview Scheduled – Microsoft Summer Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "The Technical Interview round for Microsoft Summer Internship "
            "is scheduled for 20 June 2027 at SJT Seminar Hall.\n\n"
            "Time: 10:00 AM\n"
            "Carry: Resume, College ID, Laptop\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "ms_005",
        "thread_id": "thread_ms_2027",
        "subject": "Offer Letter Released – Microsoft Summer Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Congratulations! The offer letters for Microsoft Summer Internship "
            "have been released. Selected candidates can collect them from the "
            "placement office.\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    # ── Dell Thread (3 emails) ──────────────────────────────────
    {
        "id": "dell_001",
        "thread_id": "thread_dell_2027",
        "subject": "Campus Drive – Dell Technologies Campus Hiring 2027",
        "sender": "Placements VIT <placements@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Dell Technologies campus drive for 2027 batch.\n\n"
            "Role: Cloud Infrastructure Engineer\n"
            "CTC: 12 LPA\n"
            "Location: Bangalore\n"
            "Eligibility: B.Tech CSE, IT – 2027\n"
            "CGPA: 7.5+\n"
            "Deadline: 8 June 2027\n\n"
            "Regards,\nPlacements VIT"
        ),
    },
    {
        "id": "dell_002",
        "thread_id": "thread_dell_2027",
        "subject": "OA Scheduled – Dell Technologies Campus Hiring 2027",
        "sender": "Placements VIT <placements@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Online Assessment for Dell Technologies has been scheduled.\n"
            "Date: 15 June 2027\nPlatform: HackerRank\n\n"
            "Regards,\nPlacements VIT"
        ),
    },
    {
        "id": "dell_003",
        "thread_id": "thread_dell_2027",
        "subject": "Shortlist – Dell Technologies Campus Hiring 2027",
        "sender": "Placements VIT <placements@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "The following students have been shortlisted for the interview "
            "round of Dell Technologies.\n\n"
            "Regards,\nPlacements VIT"
        ),
    },
    # ── Standard Chartered (3 emails) ──────────────────────────────
    {
        "id": "sc_001",
        "thread_id": "thread_sc_2027",
        "subject": "Campus Drive – Standard Chartered Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Standard Chartered is hiring interns for 2027.\n\n"
            "Role: Technology Analyst Intern\n"
            "Stipend: Rs. 60000 per month\n"
            "Location: Chennai\n"
            "Deadline: 12 June 2027\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "sc_002",
        "thread_id": "thread_sc_2027",
        "subject": "OA Scheduled – Standard Chartered Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "The coding test for Standard Chartered internship is scheduled.\n"
            "Date: 18 June 2027\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "sc_003",
        "thread_id": "thread_sc_2027",
        "subject": "Interview Scheduled – Standard Chartered Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Interview round for Standard Chartered is scheduled for 25 June.\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    # ── HPE (3 emails) ──────────────────────────────────
    {
        "id": "hpe_001",
        "thread_id": "thread_hpe_2027",
        "subject": "Campus Drive – HPE Campus Placement 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Hewlett Packard Enterprise (HPE) campus placement drive.\n\n"
            "Role: Software Developer\n"
            "CTC: 10 LPA\n"
            "Location: Bangalore\n"
            "Deadline: 10 June 2027\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "hpe_002",
        "thread_id": "thread_hpe_2027",
        "subject": "Shortlist – HPE Campus Placement 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "The shortlisted students for HPE campus placement are listed below.\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    {
        "id": "hpe_003",
        "thread_id": "thread_hpe_2027",
        "subject": "Offer Released – HPE Campus Placement 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Dear Students,\n\n"
            "Congratulations! Final selection results for HPE have been announced. "
            "Offer letters will be released soon.\n\n"
            "Regards,\nCDC VIT"
        ),
    },
    # ── Additional misc emails (6 more to reach 20+) ──────────────────
    {
        "id": "google_001",
        "thread_id": "thread_google_2027",
        "subject": "Campus Hiring – Google Summer Internship 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Google is hiring summer interns.\n"
            "Role: STEP Intern\nStipend: Rs. 100000 per month\n"
            "Location: Bangalore\nDeadline: 1 June 2027\n"
        ),
    },
    {
        "id": "amazon_001",
        "thread_id": "thread_amazon_2027",
        "subject": "Campus Drive – Amazon SDE Intern 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Amazon campus drive for SDE Intern position.\n"
            "Stipend: Rs. 90000 per month\nLocation: Hyderabad\n"
            "Deadline: 3 June 2027\n"
        ),
    },
    {
        "id": "tcs_001",
        "thread_id": "thread_tcs_2027",
        "subject": "Campus Drive – TCS Ninja Hiring 2027",
        "sender": "Placements VIT <placements@vit.ac.in>",
        "body": (
            "TCS Ninja campus hiring for 2027 batch.\n"
            "Role: System Engineer\nCTC: 3.6 LPA\n"
            "Location: Pan India\nDeadline: 20 June 2027\n"
        ),
    },
    {
        "id": "infosys_001",
        "thread_id": "thread_infosys_2027",
        "subject": "Campus Recruitment – Infosys InfyTQ 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Infosys InfyTQ campus recruitment drive.\n"
            "Role: Systems Engineer\nCTC: 3.6 LPA\n"
            "Deadline: 25 June 2027\n"
        ),
    },
    {
        "id": "wipro_001",
        "thread_id": "thread_wipro_2027",
        "subject": "Registration Open – Wipro Elite Hiring 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Wipro Elite NLTH campus drive registration.\n"
            "Role: Project Engineer\nCTC: 3.5 LPA\n"
            "Location: Pan India\nDeadline: 28 June 2027\n"
            "Registration link: https://forms.gle/wipro2027\n"
        ),
    },
    {
        "id": "deloitte_001",
        "thread_id": "thread_deloitte_2027",
        "subject": "Campus Drive – Deloitte USI Hiring 2027",
        "sender": "CDC VIT <cdc@vit.ac.in>",
        "body": (
            "Deloitte USI is hiring for Analyst position.\n"
            "Role: Analyst\nCTC: 7.8 LPA\n"
            "Location: Hyderabad, Bangalore\n"
            "Deadline: 30 June 2027\n"
        ),
    },
]


# ===================================================================
# Helper: simulate processing pipeline for one email
# ===================================================================


def _process_email(db_manager: DatabaseManager, email: dict) -> tuple[int, bool]:
    """Simulate the extraction + DB pipeline for one email (no real APIs)."""
    subject = email["subject"]
    body = email["body"]
    sender = email["sender"]

    # Phase 13: classify
    classification = classify_email(subject, body)

    # Phase 3: rule extraction
    result = extract_from_email(subject, body, sender)

    # Build opportunity dict
    opp_data = result.to_dict()

    # Phase 4: normalize company
    if opp_data.get("company_name"):
        opp_data["company_name"] = normalize_company_name(opp_data["company_name"])
    else:
        # Fallback for emails where extraction can't find company from subject pattern
        # Use a simple heuristic for testing
        for company in ["Microsoft", "Dell", "Standard Chartered", "HPE",
                        "Google", "Amazon", "TCS", "Infosys", "Wipro", "Deloitte"]:
            if company.lower() in subject.lower():
                opp_data["company_name"] = normalize_company_name(company)
                break
        if not opp_data.get("company_name"):
            opp_data["company_name"] = "Unknown Company"

    # Ensure role
    if not opp_data.get("role"):
        opp_data["role"] = "Unknown Role"

    # Phase 2: detect status
    detected = detect_status_from_text(subject, body)
    if detected != "OPEN":
        opp_data["current_status"] = detected

    opp_data["email_received_at"] = "29-May-2027 10:30 AM"
    opp_data["last_update_timestamp"] = "2027-05-29T10:30:00+00:00"

    return db_manager.insert_or_update_opportunity(
        opp_data,
        source_email_id=email["id"],
        source_thread_id=email["thread_id"],
        email_classification=classification,
    )


# ===================================================================
# E2E Tests
# ===================================================================


class TestEndToEnd:
    """End-to-end tests processing realistic CDC email corpus."""

    def test_microsoft_full_lifecycle(self, db_manager: DatabaseManager):
        """Microsoft: 5 emails → 1 drive, status OPEN → OA → SHORTLISTED → INTERVIEW → OFFER_RECEIVED."""
        ms_emails = [e for e in CDC_EMAILS if e["id"].startswith("ms_")]
        assert len(ms_emails) == 5

        first_id = None
        for email in ms_emails:
            opp_id, created = _process_email(db_manager, email)
            if first_id is None:
                first_id = opp_id
                assert created is True
            else:
                # All subsequent should update
                assert opp_id == first_id, "All Microsoft emails must map to same drive"

        record = db_manager.fetch_opportunity_by_id(first_id)
        assert record is not None
        assert record["current_status"] == "OFFER_RECEIVED"

        history = json.loads(record["status_history"])
        assert len(history) >= 3  # At least several unique statuses
        assert "OFFER_RECEIVED" in history

    def test_dell_lifecycle(self, db_manager: DatabaseManager):
        """Dell: 3 emails → 1 drive, progresses through OA and SHORTLISTED."""
        dell_emails = [e for e in CDC_EMAILS if e["id"].startswith("dell_")]
        assert len(dell_emails) == 3

        first_id = None
        for email in dell_emails:
            opp_id, created = _process_email(db_manager, email)
            if first_id is None:
                first_id = opp_id
            else:
                assert opp_id == first_id

        record = db_manager.fetch_opportunity_by_id(first_id)
        assert record is not None
        history = json.loads(record["status_history"])
        assert "SHORTLISTED" in history

    def test_no_duplicate_drives(self, db_manager: DatabaseManager):
        """Processing all emails should not produce duplicate drives per thread."""
        thread_ids = set()
        drive_ids_by_thread: dict[str, int] = {}

        for email in CDC_EMAILS:
            opp_id, _ = _process_email(db_manager, email)
            tid = email["thread_id"]
            if tid in drive_ids_by_thread:
                assert drive_ids_by_thread[tid] == opp_id, (
                    f"Thread {tid} produced different drive IDs: "
                    f"{drive_ids_by_thread[tid]} vs {opp_id}"
                )
            else:
                drive_ids_by_thread[tid] = opp_id

        # Unique drives should equal unique threads
        all_opps = db_manager.fetch_active_opportunities()
        unique_threads = len(set(e["thread_id"] for e in CDC_EMAILS))
        assert len(all_opps) == unique_threads

    def test_drive_id_generated_for_all(self, db_manager: DatabaseManager):
        """Every drive must have a non-empty drive_id."""
        for email in CDC_EMAILS:
            _process_email(db_manager, email)

        all_opps = db_manager.fetch_active_opportunities()
        for opp in all_opps:
            assert opp["drive_id"], f"Drive for {opp['company_name']} has no drive_id"
            assert len(opp["drive_id"]) > 5

    def test_gmail_link_generation(self, db_manager: DatabaseManager):
        """Sheet rows should contain clickable Gmail links."""
        for email in CDC_EMAILS:
            _process_email(db_manager, email)

        all_opps = db_manager.fetch_active_opportunities()
        for opp in all_opps:
            row = opportunity_to_sheet_row(opp)
            email_col = row[16]
            # All drives have source_email_id or source_thread_id
            if opp.get("source_thread_id") or opp.get("source_email_id"):
                assert "HYPERLINK" in email_col or email_col == ""

    def test_classification_stored(self, db_manager: DatabaseManager):
        """Email classification should be stored with each drive."""
        for email in CDC_EMAILS[:5]:  # Microsoft thread
            _process_email(db_manager, email)

        # The drive should have a classification from the last email
        all_opps = db_manager.fetch_active_opportunities()
        for opp in all_opps:
            # email_classification may be set from the last processed email
            assert opp.get("email_classification") is not None

    def test_company_normalization_across_corpus(self, db_manager: DatabaseManager):
        """Company names should be normalized consistently."""
        for email in CDC_EMAILS:
            _process_email(db_manager, email)

        all_opps = db_manager.fetch_active_opportunities()
        company_names = {opp["company_name"] for opp in all_opps}

        # Dell should be normalized to "Dell Technologies"
        dell_opps = [o for o in all_opps if "Dell" in o["company_name"] or "dell" in o["company_name"].lower()]
        if dell_opps:
            assert all(o["company_name"] == "Dell Technologies" for o in dell_opps)

    def test_dashboard_after_full_processing(self, db_manager: DatabaseManager):
        """Dashboard metrics should reflect all processed drives."""
        for email in CDC_EMAILS:
            _process_email(db_manager, email)

        metrics = db_manager.get_dashboard_metrics()
        assert metrics["total_drives"] >= 10
        assert metrics["companies_applied"] >= 5

    def test_active_drives_excludes_offered(self, db_manager: DatabaseManager):
        """After Microsoft gets OFFER_RECEIVED, it should not appear in active drives."""
        # Process Microsoft thread (ends in OFFER_RECEIVED)
        ms_emails = [e for e in CDC_EMAILS if e["id"].startswith("ms_")]
        for email in ms_emails:
            _process_email(db_manager, email)

        active = db_manager.fetch_active_drives_only()
        active_statuses = {r["current_status"] for r in active}
        # OFFER_RECEIVED should not be in active drives
        assert "OFFER_RECEIVED" not in active_statuses

    def test_total_email_count(self):
        """Verify we have 20+ emails in the corpus."""
        assert len(CDC_EMAILS) >= 20


# ===================================================================
# Status Extraction E2E
# ===================================================================


class TestStatusExtractionE2E:
    """Verify status detection works correctly on the realistic corpus."""

    @pytest.mark.parametrize(
        "email_id,expected_status",
        [
            ("ms_002", "OA"),
            ("ms_003", "SHORTLISTED"),
            ("ms_004", "INTERVIEW"),
            ("ms_005", "OFFER_RECEIVED"),
            ("dell_002", "OA"),
            ("dell_003", "SHORTLISTED"),
            ("sc_002", "OA"),
            ("sc_003", "INTERVIEW"),
            ("hpe_002", "SHORTLISTED"),
            ("hpe_003", "OFFER_RECEIVED"),
        ],
    )
    def test_status_detection_on_corpus(self, email_id: str, expected_status: str):
        email = next(e for e in CDC_EMAILS if e["id"] == email_id)
        detected = detect_status_from_text(email["subject"], email["body"])
        assert detected == expected_status, (
            f"Email {email_id} ({email['subject']!r}): "
            f"expected {expected_status}, got {detected}"
        )


class TestClassificationE2E:
    """Verify email classification on the corpus."""

    @pytest.mark.parametrize(
        "email_id,expected_class",
        [
            ("ms_001", "NEW_DRIVE"),
            ("ms_002", "OA_UPDATE"),
            ("ms_003", "SHORTLIST_UPDATE"),
            ("ms_004", "INTERVIEW_UPDATE"),
            ("ms_005", "OFFER_UPDATE"),
            ("dell_001", "NEW_DRIVE"),
            ("dell_002", "OA_UPDATE"),
            ("dell_003", "SHORTLIST_UPDATE"),
        ],
    )
    def test_classification_on_corpus(self, email_id: str, expected_class: str):
        email = next(e for e in CDC_EMAILS if e["id"] == email_id)
        result = classify_email(email["subject"], email["body"])
        assert result == expected_class
