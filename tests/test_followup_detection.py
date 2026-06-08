"""Phase 1, 2, 5: Follow-up detection, drive ID generation, and deduplication.

Tests that:
- Sequential emails in the same thread update ONE drive (no duplicates).
- Different roles for the same company create separate drives.
- Drive IDs follow the ``COMPANY_YEAR_ROLE_CATEGORY`` pattern.
- Status history accumulates correctly.
- Company name normalization merges variant spellings.
"""

from __future__ import annotations

import json

import pytest

from placement_mail_tracker.db.manager import (
    DatabaseManager,
    generate_drive_id,
    generate_unique_hash,
)
from placement_mail_tracker.extraction.rule_engine import normalize_company_name

# ===================================================================
# Thread-based follow-up detection
# ===================================================================


class TestThreadFollowupDetection:
    """Phase 1+2: Emails sharing a Gmail thread_id must update one drive."""

    def test_thread_followup_detection(self, db_manager: DatabaseManager):
        """Tata Motors: OA → Shortlist → Interview → single record with status_history."""
        thread_id = "thread_tata_motors_001"

        # Email 1: New drive announcement (OA)
        opp1 = {
            "company_name": "Tata Motors",
            "role": "Graduate Engineer Trainee",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "8 LPA",
            "current_status": "OA",
        }
        id1, created1 = db_manager.insert_or_update_opportunity(
            opp1, source_email_id="msg_001", source_thread_id=thread_id,
        )
        assert created1 is True

        # Email 2: Shortlist in same thread
        opp2 = {
            "company_name": "Tata Motors",
            "role": "Graduate Engineer Trainee",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "8 LPA",
            "current_status": "SHORTLISTED",
        }
        id2, created2 = db_manager.insert_or_update_opportunity(
            opp2, source_email_id="msg_002", source_thread_id=thread_id,
        )
        assert created2 is False, "Second email should update, not create"
        assert id2 == id1, "Same drive ID expected"

        # Email 3: Interview in same thread
        opp3 = {
            "company_name": "Tata Motors",
            "role": "Graduate Engineer Trainee",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "8 LPA",
            "current_status": "INTERVIEW",
        }
        id3, created3 = db_manager.insert_or_update_opportunity(
            opp3, source_email_id="msg_003", source_thread_id=thread_id,
        )
        assert created3 is False
        assert id3 == id1

        # Verify status history
        record = db_manager.fetch_opportunity_by_id(id1)
        assert record is not None
        history = json.loads(record["status_history"])
        assert "OA" in history
        assert "SHORTLISTED" in history
        assert "INTERVIEW" in history
        assert record["current_status"] == "INTERVIEW"

    def test_status_history_accumulation(self, db_manager: DatabaseManager):
        """Status history should not duplicate consecutive identical statuses."""
        thread_id = "thread_acc_001"

        opp_base = {
            "company_name": "Dell Technologies",
            "role": "Cloud Engineer",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "12 LPA",
            "current_status": "OPEN",
        }

        id1, _ = db_manager.insert_or_update_opportunity(
            opp_base, source_email_id="msg_a1", source_thread_id=thread_id,
        )

        # Send same status again
        id2, _ = db_manager.insert_or_update_opportunity(
            {**opp_base, "current_status": "OPEN"},
            source_email_id="msg_a2",
            source_thread_id=thread_id,
        )
        assert id1 == id2

        record = db_manager.fetch_opportunity_by_id(id1)
        history = json.loads(record["status_history"])
        # OPEN should appear only once since it's the same consecutive status
        assert history.count("OPEN") == 1


# ===================================================================
# Separate drives for different roles
# ===================================================================


class TestSeparateDrives:
    """Same company but different roles → two distinct drives."""

    def test_separate_drives_different_roles(self, db_manager: DatabaseManager):
        opp_sde = {
            "company_name": "Microsoft",
            "role": "Software Engineer Intern",
            "internship_or_fulltime": "internship",
            "package_or_stipend": "50000 per month",
            "current_status": "OPEN",
        }
        opp_ds = {
            "company_name": "Microsoft",
            "role": "Data Scientist Intern",
            "internship_or_fulltime": "internship",
            "package_or_stipend": "50000 per month",
            "current_status": "OPEN",
        }

        id1, created1 = db_manager.insert_or_update_opportunity(
            opp_sde, source_email_id="ms_sde_001",
        )
        id2, created2 = db_manager.insert_or_update_opportunity(
            opp_ds, source_email_id="ms_ds_001",
        )

        assert created1 is True
        assert created2 is True
        assert id1 != id2, "Different roles must produce different drives"


# ===================================================================
# Drive ID generation (Phase 5)
# ===================================================================


class TestDriveIdGeneration:
    """Drive IDs should encode company, year, role, and category."""

    def test_drive_id_format_intern(self):
        drive_id = generate_drive_id(
            "Microsoft",
            role="Software Engineer Intern",
            category="internship",
        )
        parts = drive_id.split("_")
        assert "MICROSOFT" in parts[0]
        assert parts[1].isdigit()  # year
        assert "INTERN" in drive_id

    def test_drive_id_format_fte(self):
        drive_id = generate_drive_id(
            "Dell Technologies",
            role="Cloud Engineer",
            category="full_time",
        )
        assert "DELL" in drive_id.upper()
        assert "FTE" in drive_id

    def test_drive_id_no_role(self):
        drive_id = generate_drive_id("Standard Chartered")
        assert "STANDARDCHARTERED" in drive_id

    def test_unique_hash_deterministic(self):
        opp = {
            "company_name": "Microsoft",
            "role": "SDE Intern",
            "package_or_stipend": "50K pm",
        }
        h1 = generate_unique_hash(opp)
        h2 = generate_unique_hash(opp)
        assert h1 == h2, "Same input must produce the same hash"

    def test_unique_hash_differs_for_different_roles(self):
        opp_a = {"company_name": "Microsoft", "role": "SDE Intern", "package_or_stipend": "50K"}
        opp_b = {"company_name": "Microsoft", "role": "DS Intern", "package_or_stipend": "50K"}
        assert generate_unique_hash(opp_a) != generate_unique_hash(opp_b)


# ===================================================================
# Company normalization dedup (Phase 4)
# ===================================================================


class TestCompanyNormalizationDedup:
    """Variant spellings of a company must resolve to the same canonical name."""

    @pytest.mark.parametrize(
        "variant",
        [
            "TATA MOTORS",
            "Tata Motors",
            "tata motors",
            "Tata Motors Ltd.",
            "Tata Motors Limited",
        ],
    )
    def test_tata_motors_variants(self, variant: str):
        assert normalize_company_name(variant) == "Tata Motors"

    @pytest.mark.parametrize(
        "variant",
        [
            "DELL",
            "Dell",
            "dell",
            "Dell Technologies",
            "DELL TECHNOLOGIES",
        ],
    )
    def test_dell_variants(self, variant: str):
        assert normalize_company_name(variant) == "Dell Technologies"

    def test_normalization_in_db_context(self, db_manager: DatabaseManager):
        """Drives from variant company names should resolve to one canonical name in DB."""
        thread = "thread_dedup_001"
        opp1 = {
            "company_name": "TATA MOTORS",
            "role": "GET",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "8 LPA",
            "current_status": "OPEN",
        }
        id1, _ = db_manager.insert_or_update_opportunity(
            opp1, source_email_id="dedup_1", source_thread_id=thread,
        )

        opp2 = {
            "company_name": "Tata Motors",
            "role": "GET",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "8 LPA",
            "current_status": "OA",
        }
        id2, created = db_manager.insert_or_update_opportunity(
            opp2, source_email_id="dedup_2", source_thread_id=thread,
        )

        assert id1 == id2, "Same canonical company + thread should not duplicate"
        record = db_manager.fetch_opportunity_by_id(id1)
        assert record["company_name"] == "Tata Motors"
