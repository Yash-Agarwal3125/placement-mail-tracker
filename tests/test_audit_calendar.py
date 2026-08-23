"""Tests for scripts/audit_calendar.py (safety-nets plan, Phase 2 + 4).

Never touches the live database -- operates on the in-memory ``db_manager``
fixture only. Read-only script: no commit/write assertions needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import audit_calendar as audit  # noqa: E402

from placement_mail_tracker.config.settings import Settings  # noqa: E402


def _seed(db_manager, **overrides):
    base = {
        "company_name": "Honeywell Aerospace",
        "role": "Intern",
        "current_status": "OA",
        "eligibility_status": "ELIGIBLE",
    }
    base.update(overrides)
    opp_id, _ = db_manager.insert_or_update_opportunity(
        base, source_email_id=overrides.get("source_email_id", f"seed-{base['company_name']}")
    )
    return opp_id


class TestAuditActiveEvents:
    def test_eligible_applied_drive_with_oa_date_survives(self, db_manager, mock_settings):
        _seed(
            db_manager,
            company_name="Honeywell Aerospace",
            oa_date="2026-08-18T14:30",
            my_status="APPLIED",
        )
        survived, missing = audit.audit_active_events(db_manager, mock_settings)
        assert len(survived) == 1
        row = survived[0]
        assert row["company"] == "Honeywell Aerospace"
        assert row["event_type"] == "OA"
        assert row["drive_kind"] == "PLACEMENT"
        assert "my_status=APPLIED" in row["reason"] or "all_eligible" in row["reason"]

    def test_webinar_drive_kind_excluded_with_reason(self, db_manager, mock_settings):
        _seed(
            db_manager,
            company_name="Deloitte",
            drive_kind="WEBINAR",
            oa_date="2026-08-24T10:00",
        )
        survived, missing = audit.audit_active_events(db_manager, mock_settings)
        assert survived == []
        assert len(missing) == 1
        assert missing[0]["company"] == "Deloitte"
        assert "WEBINAR" in missing[0]["reason"]

    def test_not_eligible_drive_excluded_with_reason(self, db_manager, mock_settings):
        _seed(
            db_manager,
            company_name="NotForMe Corp",
            eligibility_status="NOT_ELIGIBLE",
            oa_date="2026-08-24T10:00",
        )
        survived, missing = audit.audit_active_events(db_manager, mock_settings)
        assert survived == []
        assert "NOT_ELIGIBLE" in missing[0]["reason"]

    def test_roster_excluded_round_reported_with_verdict(self, db_manager, mock_settings):
        opp_id = _seed(
            db_manager,
            company_name="Flipkart",
            oa_date="2026-08-10T10:00",
            interview_date="2026-08-20T10:00",
            my_status="APPLIED",
        )
        db_manager.upsert_roster_verdict(opp_id, "OA", "NOT_MATCHED", method="test")

        survived, missing = audit.audit_active_events(db_manager, mock_settings)
        interview_missing = [m for m in missing if m["event_type"] == "INTERVIEW"]
        assert len(interview_missing) == 1
        assert "NOT_MATCHED" in interview_missing[0]["reason"]
        assert "OA" in interview_missing[0]["reason"]

    def test_no_date_at_all_produces_neither_survived_nor_missing(
        self, db_manager, mock_settings
    ):
        _seed(db_manager, company_name="NoDates Inc")
        survived, missing = audit.audit_active_events(db_manager, mock_settings)
        assert survived == []
        assert missing == []

    def test_collision_dropped_event_does_not_claim_unparseable(
        self, db_manager, mock_settings
    ):
        """Two opportunities that normalise to the same company + event_type
        + date collide in _apply_collision_guard; the loser has a perfectly
        parseable date and passes every other gate, so the exclusion reason
        must not falsely claim the date was unparseable."""
        _seed(
            db_manager,
            company_name="Collide Co",
            role="Role A",
            oa_date="2026-08-20T10:00",
            my_status="APPLIED",
            source_email_id="seed-collide-a",
        )
        _seed(
            db_manager,
            company_name="Collide Co",
            role="Role B",
            oa_date="2026-08-20T10:00",
            my_status="APPLIED",
            source_email_id="seed-collide-b",
        )

        survived, missing = audit.audit_active_events(db_manager, mock_settings)
        assert len(survived) == 1
        dropped = [m for m in missing if m["company"] is not None]
        assert len(dropped) == 1
        assert "unparseable" not in dropped[0]["reason"]
        assert "collision" in dropped[0]["reason"].lower()

    def test_unparseable_date_surfaces_as_anomaly(self, db_manager, mock_settings):
        _seed(
            db_manager,
            company_name="BadDate Co",
            oa_date="not a real date at all",
            my_status="APPLIED",
        )
        survived, missing = audit.audit_active_events(db_manager, mock_settings)
        assert survived == []
        assert any(m["company"] is None and "anomaly" in m["reason"] for m in missing)


class TestReverseReconciliation:
    def test_skips_gracefully_when_no_token_file(self, db_manager, tmp_path):
        settings = Settings(
            APP_ENV="testing", CALENDAR_TOKEN_FILE=str(tmp_path / "no_such_token.json")
        )
        orphans, skip_reason = audit.audit_reverse_reconciliation(db_manager, settings)
        assert orphans is None
        assert skip_reason is not None
        assert "token" in skip_reason.lower()

    def test_reports_orphan_event_with_no_backing_row(self, db_manager, tmp_path):
        token_path = tmp_path / "calendar_token.json"
        token_path.write_text("{}", encoding="utf-8")
        settings = Settings(APP_ENV="testing", CALENDAR_TOKEN_FILE=str(token_path))

        db_manager.upsert_calendar_event_state(
            _fake_event(opportunity_id=1, event_type="OA"),
            gcal_calendar_id="cal_vit",
            gcal_event_id="evt_known",
        )

        fake_client = MagicMock()
        fake_client.find_calendar_id.return_value = "cal_vit"
        fake_client.list_events.return_value = [
            {"id": "evt_known", "summary": "Known — OA", "start": {"dateTime": "2026-08-18"}},
            {"id": "evt_orphan", "summary": "WorkIndia", "start": {"date": "2026-08-19"}},
        ]

        with patch(
            "placement_mail_tracker.calendar_sync.client.GoogleCalendarClient",
            return_value=fake_client,
        ):
            orphans, skip_reason = audit.audit_reverse_reconciliation(db_manager, settings)

        assert skip_reason is None
        assert len(orphans) == 1
        assert orphans[0]["gcal_event_id"] == "evt_orphan"
        assert orphans[0]["summary"] == "WorkIndia"

    def test_never_deletes_anything(self, db_manager, tmp_path):
        """Report-only contract: the client's delete_event must never be
        called by the audit path, even when orphans are found."""
        token_path = tmp_path / "calendar_token.json"
        token_path.write_text("{}", encoding="utf-8")
        settings = Settings(APP_ENV="testing", CALENDAR_TOKEN_FILE=str(token_path))

        fake_client = MagicMock()
        fake_client.find_calendar_id.return_value = "cal_vit"
        fake_client.list_events.return_value = [
            {"id": "evt_orphan", "summary": "Orphan", "start": {"date": "2026-08-19"}},
        ]

        with patch(
            "placement_mail_tracker.calendar_sync.client.GoogleCalendarClient",
            return_value=fake_client,
        ):
            audit.audit_reverse_reconciliation(db_manager, settings)

        fake_client.delete_event.assert_not_called()
        fake_client.insert_event.assert_not_called()
        fake_client.patch_event.assert_not_called()


def _fake_event(*, opportunity_id: int, event_type: str):
    from placement_mail_tracker.calendar_sync.derive import CalendarEvent

    return CalendarEvent(
        opportunity_id=opportunity_id,
        drive_id="DRV_1",
        event_type=event_type,
        title="Known — OA",
        start_iso="2026-08-18T10:00:00+05:30",
        end_iso="2026-08-18T11:00:00+05:30",
        all_day=False,
        location=None,
        description="",
        reminder_minutes=[60],
    )
