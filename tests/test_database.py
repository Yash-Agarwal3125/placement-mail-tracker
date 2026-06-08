"""Phase 7, 11, 12: Database manager tests.

Covers:
- Insert and update opportunities
- Action Required computation (deadline-aware)
- Personal status tracking (my_status)
- Dashboard metrics
- Active drives filter (excludes REJECTED, WITHDRAWN, etc.)
- Retry queue (PENDING_EXTRACTION)
- Gmail message_id storage
"""

from __future__ import annotations

from datetime import datetime, timedelta

from placement_mail_tracker.db.manager import DatabaseManager

# ===================================================================
# Insert / Update
# ===================================================================


class TestInsertOpportunity:
    """Basic CRUD for opportunities."""

    def test_insert_opportunity(self, db_manager: DatabaseManager, sample_opportunity):
        opp = sample_opportunity("Google", "SDE Intern")
        opp_id, created = db_manager.insert_or_update_opportunity(
            opp, source_email_id="goog_001",
        )
        assert created is True
        assert opp_id > 0

        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record is not None
        assert record["company_name"] == "Google"
        assert record["role"] == "SDE Intern"
        assert record["current_status"] == "OPEN"

    def test_insert_sets_drive_id(self, db_manager: DatabaseManager, sample_opportunity):
        opp = sample_opportunity("Amazon", "Cloud Engineer", "full_time")
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="amz_001")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["drive_id"] is not None
        assert "AMAZON" in record["drive_id"]

    def test_update_opportunity(self, db_manager: DatabaseManager, sample_opportunity):
        opp = sample_opportunity("Infosys", "SE Trainee", "full_time")
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="inf_001")

        # Update with new status via thread
        updated = {**opp, "current_status": "OA"}
        opp_id2, created = db_manager.insert_or_update_opportunity(
            updated, source_email_id="inf_002",
        )
        assert opp_id == opp_id2
        assert created is False

        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["current_status"] == "OA"


# ===================================================================
# Phase 11: Action Required
# ===================================================================


class TestActionRequired:
    """Verify the action_required engine produces correct labels."""

    def test_apply_today_deadline_tomorrow(self, db_manager: DatabaseManager):
        tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
        opp = {
            "company_name": "TestCo",
            "role": "Intern",
            "internship_or_fulltime": "internship",
            "package_or_stipend": "30K pm",
            "deadline": tomorrow,
            "current_status": "OPEN",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="act_001")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] == "APPLY TODAY"

    def test_prepare_for_test_oa_tomorrow(self, db_manager: DatabaseManager):
        tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
        opp = {
            "company_name": "TestCo OA",
            "role": "Engineer",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "10 LPA",
            "oa_date": tomorrow,
            "current_status": "OA",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="act_002")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] == "PREPARE FOR TEST"

    def test_prepare_for_interview_tomorrow(self, db_manager: DatabaseManager):
        tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
        opp = {
            "company_name": "TestCo Interview",
            "role": "Analyst",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "9 LPA",
            "interview_date": tomorrow,
            "current_status": "INTERVIEW",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="act_003")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] == "PREPARE FOR INTERVIEW"

    def test_review_offer(self, db_manager: DatabaseManager):
        opp = {
            "company_name": "TestCo Offer",
            "role": "Manager",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "20 LPA",
            "current_status": "OFFER_RECEIVED",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="act_004")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] == "REVIEW OFFER"

    def test_register_before_deadline(self, db_manager: DatabaseManager):
        far_future = (datetime.now() + timedelta(days=10)).isoformat()
        opp = {
            "company_name": "TestCo Reg",
            "role": "Trainee",
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "6 LPA",
            "deadline": far_future,
            "current_status": "OPEN",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="act_005")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] == "REGISTER BEFORE DEADLINE"

    def test_no_action_no_deadline(self, db_manager: DatabaseManager):
        opp = {
            "company_name": "TestCo No",
            "role": "Dev",
            "internship_or_fulltime": "internship",
            "package_or_stipend": "40K pm",
            "current_status": "OA",
        }
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="act_006")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["action_required"] is None


# ===================================================================
# Phase 12: My Status Tracking
# ===================================================================


class TestMyStatusTracking:
    def test_default_not_applied(self, db_manager: DatabaseManager, sample_opportunity):
        opp = sample_opportunity("Wipro", "SE")
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="my_001")
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["my_status"] == "NOT_APPLIED"


# ===================================================================
# Phase 10: Dashboard Metrics
# ===================================================================


class TestDashboardMetrics:
    def test_dashboard_metrics(self, db_manager: DatabaseManager, sample_opportunity):
        # Insert a mix of statuses
        opps = [
            sample_opportunity("A Co", "R1", current_status="OPEN"),
            sample_opportunity("B Co", "R2", current_status="OPEN"),
            sample_opportunity("C Co", "R3", current_status="OA"),
            sample_opportunity("D Co", "R4", current_status="INTERVIEW"),
            sample_opportunity("E Co", "R5", current_status="OFFER_RECEIVED"),
        ]
        for i, opp in enumerate(opps):
            db_manager.insert_or_update_opportunity(opp, source_email_id=f"dash_{i}")

        metrics = db_manager.get_dashboard_metrics()
        assert metrics["total_drives"] == 5
        assert metrics["applications_open"] == 2
        assert metrics["offers_received"] == 1
        assert metrics["companies_applied"] == 5
        assert "%" in metrics["selection_rate"]

    def test_empty_dashboard(self, db_manager: DatabaseManager):
        metrics = db_manager.get_dashboard_metrics()
        assert metrics["total_drives"] == 0
        assert metrics["selection_rate"] == "0%"


# ===================================================================
# Active Drives Only (Phase 9)
# ===================================================================


class TestActiveDrivesOnly:
    def test_active_drives_only(self, db_manager: DatabaseManager, sample_opportunity):
        # OPEN and INTERVIEW should appear; REJECTED and OFFER_RECEIVED should not
        for status in ("OPEN", "INTERVIEW", "REJECTED", "OFFER_RECEIVED"):
            opp = sample_opportunity(f"Co_{status}", f"Role_{status}", current_status=status)
            db_manager.insert_or_update_opportunity(opp, source_email_id=f"active_{status}")

        active = db_manager.fetch_active_drives_only()
        active_statuses = {r["current_status"] for r in active}

        assert "OPEN" in active_statuses
        assert "INTERVIEW" in active_statuses
        assert "REJECTED" not in active_statuses
        assert "OFFER_RECEIVED" not in active_statuses


# ===================================================================
# Retry Queue
# ===================================================================


class TestRetryQueue:
    def test_retry_queue(self, db_manager: DatabaseManager):
        """Emails with PENDING_EXTRACTION should be queryable for retry."""
        db_manager.log_processed_email(
            gmail_message_id="retry_001",
            subject="Failing email",
            sender="test@example.com",
            processed_status="PENDING_EXTRACTION",
            error_message="Gemini timeout",
        )

        rows = db_manager.connection.execute(
            """
            SELECT gmail_message_id
            FROM processed_emails
            WHERE processed_status = 'PENDING_EXTRACTION';
            """
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["gmail_message_id"] == "retry_001"


# ===================================================================
# Phase 7: Gmail IDs Stored
# ===================================================================


class TestGmailIdsStored:
    def test_gmail_ids_stored(self, db_manager: DatabaseManager, sample_opportunity):
        opp = sample_opportunity("Meta", "ML Engineer", "full_time")
        opp_id, _ = db_manager.insert_or_update_opportunity(
            opp,
            source_email_id="gmail_msg_12345",
            source_thread_id="gmail_thread_67890",
        )
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["source_email_id"] == "gmail_msg_12345"
        assert record["source_thread_id"] == "gmail_thread_67890"

    def test_processed_email_stores_classification(self, db_manager: DatabaseManager):
        db_manager.log_processed_email(
            gmail_message_id="class_001",
            subject="OA Scheduled – Test Co",
            sender="cdc@vit.ac.in",
            processed_status="processed",
            email_classification="OA_UPDATE",
        )
        row = db_manager.connection.execute(
            "SELECT email_classification FROM processed_emails WHERE gmail_message_id = ?",
            ("class_001",),
        ).fetchone()
        assert row["email_classification"] == "OA_UPDATE"


# ===================================================================
# Edge Cases
# ===================================================================


class TestEdgeCases:
    def test_insert_with_none_fields(self, db_manager: DatabaseManager):
        opp = {
            "company_name": "Edge Co",
            "role": "Tester",
            "internship_or_fulltime": None,
            "package_or_stipend": None,
            "eligibility": None,
            "cgpa_requirement": None,
            "branches_allowed": None,
            "deadline": None,
            "current_status": "OPEN",
        }
        opp_id, created = db_manager.insert_or_update_opportunity(opp, source_email_id="edge_001")
        assert created is True
        record = db_manager.fetch_opportunity_by_id(opp_id)
        assert record["company_name"] == "Edge"

    def test_multiple_updates_tracked(self, db_manager: DatabaseManager, sample_opportunity):
        opp = sample_opportunity("Track Co", "Dev")
        opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_email_id="track_001")

        events = db_manager.fetch_updates_for_opportunity(opp_id)
        assert len(events) >= 1  # At least the "created" event
        assert any(e["update_type"] == "created" for e in events)
