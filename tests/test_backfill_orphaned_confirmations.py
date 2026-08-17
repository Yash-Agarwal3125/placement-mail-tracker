"""Tests for scripts/backfill_orphaned_confirmations.py (Phase 6, calendar-
drift remediation plan): the one-off catch-up for APPLICATION_CONFIRMATION
mails logged with ``opportunity_id = NULL`` before Phase 6 shipped.

Never touches the live database -- operates on the in-memory ``db_manager``
fixture only, per CLAUDE.md and the task's explicit "build and unit-test
only, do not run against data/placement_mail_tracker.db" constraint.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backfill_orphaned_confirmations as backfill  # noqa: E402


def _seed_orphan(
    db_manager, *, msg_id: str, subject: str, received_at: str = "17-Aug-2026 10:00 AM"
):
    db_manager.log_processed_email(
        gmail_message_id=msg_id,
        subject=subject,
        sender="noreply.cdcinfo@vitstudent.ac.in",
        received_at=received_at,
        processed_status="processed",
        email_classification="APPLICATION_CONFIRMATION",
    )


def _seed_drive(db_manager, **overrides):
    base = {
        "company_name": "Honeywell",
        "role": "Aerospace Placement Drive",
        "current_status": "OPEN",
        "eligibility_status": "ELIGIBLE",
        "email_received_at": "13-Aug-2026 10:00 AM",
    }
    base.update(overrides)
    opp_id, _ = db_manager.insert_or_update_opportunity(
        base, source_email_id=overrides.get("source_email_id", f"seed-{base['company_name']}")
    )
    return opp_id


class TestFetchOrphanedConfirmations:
    def test_only_null_opportunity_confirmation_rows_are_fetched(self, db_manager):
        _seed_orphan(db_manager, msg_id="c1", subject="Confirmed: Your Registration for Accenture")
        # A non-confirmation email must never be picked up.
        db_manager.log_processed_email(
            gmail_message_id="c2",
            subject="Accenture OA Scheduled",
            processed_status="processed",
            email_classification="OA_UPDATE",
        )
        # An already-linked confirmation must never be picked up either.
        opp_id = _seed_drive(db_manager, company_name="Deloitte")
        db_manager.log_processed_email(
            gmail_message_id="c3",
            subject="Confirmed: Your Registration for Deloitte",
            opportunity_id=opp_id,
            processed_status="processed",
            email_classification="APPLICATION_CONFIRMATION",
        )

        rows = backfill.fetch_orphaned_confirmations(db_manager)
        assert [r["gmail_message_id"] for r in rows] == ["c1"]


class TestPlanBackfill:
    def test_no_identifiable_company_plans_unmatched(self, db_manager):
        _seed_orphan(db_manager, msg_id="c1", subject="Application Confirmation")
        plans = backfill.plan_backfill(db_manager)
        assert len(plans) == 1
        assert plans[0]["action"] == "unmatched"
        assert plans[0]["extracted_company"] is None

    def test_zero_candidates_plans_create(self, db_manager):
        _seed_orphan(
            db_manager,
            msg_id="c1",
            subject="Congratulations! You're Eligible for Honeywell Aerospace Placement Drive",
        )
        plans = backfill.plan_backfill(db_manager)
        assert len(plans) == 1
        assert plans[0]["action"] == "create"
        assert plans[0]["extracted_company"] == "Honeywell Aerospace"

    def test_single_active_candidate_plans_attach(self, db_manager):
        drive_id = _seed_drive(db_manager, company_name="Honeywell Aerospace")
        _seed_orphan(
            db_manager,
            msg_id="c1",
            subject="Confirmed: Your Registration for Honeywell Aerospace",
        )
        plans = backfill.plan_backfill(db_manager)
        assert len(plans) == 1
        assert plans[0]["action"] == "attach"
        assert plans[0]["target_id"] == drive_id

    def test_two_active_candidates_plans_unmatched(self, db_manager):
        _seed_drive(db_manager, company_name="Honeywell", role="Super Dream Internship")
        _seed_drive(
            db_manager,
            company_name="Honeywell",
            role="Aerospace Placement Drive",
            source_email_id="seed-honeywell-2",
        )
        _seed_orphan(
            db_manager, msg_id="c1", subject="Confirmed: Your Registration for Honeywell"
        )
        plans = backfill.plan_backfill(db_manager)
        assert len(plans) == 1
        assert plans[0]["action"] == "unmatched"
        assert len(plans[0]["candidate_ids"]) == 2


class TestApplyBackfill:
    def test_dry_run_helper_writes_nothing(self, db_manager):
        """plan_backfill itself must never mutate -- only read."""
        _seed_orphan(
            db_manager, msg_id="c1", subject="Confirmed: Your Registration for Accenture"
        )
        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        backfill.plan_backfill(db_manager)
        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert after == before

    def test_apply_creates_drive_and_sets_applied(self, db_manager):
        _seed_orphan(
            db_manager,
            msg_id="c1",
            subject="Congratulations! You're Eligible for Honeywell Aerospace Placement Drive",
        )
        stats = backfill.apply_backfill(db_manager)
        assert stats["created"] == 1
        assert stats["applied_status_set"] == 1

        row = db_manager.connection.execute(
            "SELECT * FROM opportunities WHERE company_name = 'Honeywell Aerospace';"
        ).fetchone()
        assert row is not None
        assert row["my_status"] == "APPLIED"

        linked = db_manager.connection.execute(
            "SELECT opportunity_id FROM processed_emails WHERE gmail_message_id = 'c1';"
        ).fetchone()
        assert linked["opportunity_id"] == row["id"]

    def test_apply_attaches_to_existing_drive_not_creates(self, db_manager):
        drive_id = _seed_drive(db_manager, company_name="Honeywell Aerospace")
        _seed_orphan(
            db_manager,
            msg_id="c1",
            subject="Confirmed: Your Registration for Honeywell Aerospace",
        )
        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        stats = backfill.apply_backfill(db_manager)

        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert after == before
        assert stats["attached"] == 1
        assert stats["created"] == 0

        row = db_manager.fetch_opportunity_by_id(drive_id)
        assert row["my_status"] == "APPLIED"

    def test_apply_never_downgrades_already_advanced_status(self, db_manager):
        drive_id = _seed_drive(db_manager, company_name="Honeywell Aerospace")
        db_manager.set_my_status(
            db_manager.fetch_opportunity_by_id(drive_id)["drive_id"],
            "SHORTLISTED",
            source="sheet",
        )
        _seed_orphan(
            db_manager,
            msg_id="c1",
            subject="Confirmed: Your Registration for Honeywell Aerospace",
        )
        backfill.apply_backfill(db_manager)
        row = db_manager.fetch_opportunity_by_id(drive_id)
        assert row["my_status"] == "SHORTLISTED"

    def test_apply_routes_unresolvable_company_to_unmatched_confirmations(self, db_manager):
        _seed_orphan(db_manager, msg_id="c1", subject="Application Confirmation")
        stats = backfill.apply_backfill(db_manager)
        assert stats["unmatched"] == 1
        unmatched = db_manager.fetch_unmatched_confirmations()
        assert len(unmatched) == 1
        assert unmatched[0]["gmail_message_id"] == "c1"

    def test_apply_routes_ambiguous_candidates_to_unmatched_confirmations(self, db_manager):
        _seed_drive(db_manager, company_name="Honeywell", role="Super Dream Internship")
        _seed_drive(
            db_manager,
            company_name="Honeywell",
            role="Aerospace Placement Drive",
            source_email_id="seed-honeywell-2",
        )
        _seed_orphan(
            db_manager, msg_id="c1", subject="Confirmed: Your Registration for Honeywell"
        )
        before = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        stats = backfill.apply_backfill(db_manager)

        after = db_manager.connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert after == before
        assert stats["unmatched"] == 1
        unmatched = db_manager.fetch_unmatched_confirmations()
        assert len(unmatched) == 1
