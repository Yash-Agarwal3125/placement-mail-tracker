"""ADR-D8 / Decision 6: My Status read-back write-back into SQLite.

Covers:
- DatabaseManager.bulk_update_my_status: writes, skips no-ops, ignores blanks.
- sheets_sync._my_status_to_enum: friendly-label and raw-enum reverse mapping.
- GoogleSheetsSync._read_my_status_map: a genuine API failure propagates
  (B2 fix) instead of being swallowed into an empty dict that would wipe
  every user-set status on the next clear-and-write.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.sheets.sheets_sync import GoogleSheetsSync, _my_status_to_enum


class TestBulkUpdateMyStatus:
    def test_writes_new_value(self, db_manager: DatabaseManager, sample_opportunity):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Microsoft", "SDE Intern"), source_email_id="msg_1",
        )
        drive_id = db_manager.fetch_opportunity_by_id(opp_id)["drive_id"]

        changed = db_manager.bulk_update_my_status({drive_id: "APPLIED"})

        assert changed == 1
        assert db_manager.fetch_opportunity_by_id(opp_id)["my_status"] == "APPLIED"

    def test_skips_noop_when_value_unchanged(self, db_manager: DatabaseManager, sample_opportunity):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Dell", "Intern"), source_email_id="msg_2",
        )
        drive_id = db_manager.fetch_opportunity_by_id(opp_id)["drive_id"]
        db_manager.bulk_update_my_status({drive_id: "APPLIED"})

        changed = db_manager.bulk_update_my_status({drive_id: "APPLIED"})

        assert changed == 0

    def test_ignores_blank_drive_id_or_status(self, db_manager: DatabaseManager):
        assert db_manager.bulk_update_my_status({"": "APPLIED", "SOME_ID": ""}) == 0

    def test_unknown_drive_id_is_a_noop(self, db_manager: DatabaseManager):
        assert db_manager.bulk_update_my_status({"NOT_A_REAL_DRIVE": "APPLIED"}) == 0


class TestSetMyStatusLadder:
    """D4 (docs/design/10-confirmation-and-reminders.md): source='automation'
    enforces an upgrade-only ladder; source='sheet' stays fully authoritative,
    including downgrades — unchanged from the pre-D4 behaviour."""

    def _seed(self, db_manager: DatabaseManager, sample_opportunity, my_status="NOT_APPLIED"):
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("LadderCo", "SDE Intern"), source_email_id=f"seed_{my_status}",
        )
        drive_id = db_manager.fetch_opportunity_by_id(opp_id)["drive_id"]
        if my_status != "NOT_APPLIED":
            db_manager.set_my_status(drive_id, my_status, source="sheet")
        return drive_id

    def test_automation_upgrades_not_applied_to_applied(
        self, db_manager: DatabaseManager, sample_opportunity
    ):
        drive_id = self._seed(db_manager, sample_opportunity)

        changed = db_manager.set_my_status(drive_id, "APPLIED", source="automation")

        assert changed is True
        row = db_manager.connection.execute(
            "SELECT my_status FROM opportunities WHERE drive_id = ?", (drive_id,)
        ).fetchone()
        assert row["my_status"] == "APPLIED"

    def test_automation_cannot_downgrade(self, db_manager: DatabaseManager, sample_opportunity):
        drive_id = self._seed(db_manager, sample_opportunity, my_status="SHORTLISTED")

        changed = db_manager.set_my_status(drive_id, "APPLIED", source="automation")

        assert changed is False
        row = db_manager.connection.execute(
            "SELECT my_status FROM opportunities WHERE drive_id = ?", (drive_id,)
        ).fetchone()
        assert row["my_status"] == "SHORTLISTED"

    def test_duplicate_confirmation_is_a_noop(
        self, db_manager: DatabaseManager, sample_opportunity
    ):
        """D6: message-ID dedup + ladder idempotency is the whole dedup design
        -- a second automation write of the same target status is a no-op."""
        drive_id = self._seed(db_manager, sample_opportunity)

        first = db_manager.set_my_status(drive_id, "APPLIED", source="automation")
        second = db_manager.set_my_status(drive_id, "APPLIED", source="automation")

        assert first is True
        assert second is False

    def test_shortlisted_drive_plus_late_confirmation_is_a_noop(
        self, db_manager: DatabaseManager, sample_opportunity
    ):
        drive_id = self._seed(db_manager, sample_opportunity, my_status="SHORTLISTED")

        changed = db_manager.set_my_status(drive_id, "APPLIED", source="automation")

        assert changed is False

    def test_sheet_source_can_downgrade(self, db_manager: DatabaseManager, sample_opportunity):
        drive_id = self._seed(db_manager, sample_opportunity, my_status="SHORTLISTED")

        changed = db_manager.set_my_status(drive_id, "NOT_APPLIED", source="sheet")

        assert changed is True
        row = db_manager.connection.execute(
            "SELECT my_status FROM opportunities WHERE drive_id = ?", (drive_id,)
        ).fetchone()
        assert row["my_status"] == "NOT_APPLIED"

    def test_unknown_drive_id_is_a_noop_for_automation(self, db_manager: DatabaseManager):
        assert db_manager.set_my_status("NOT_A_REAL_DRIVE", "APPLIED", source="automation") is False


class TestMyStatusToEnum:
    @pytest.mark.parametrize(
        "display,expected",
        [
            ("Applied", "APPLIED"),
            ("applied", "APPLIED"),
            ("Shortlisted", "SHORTLISTED"),
            ("OA cleared", "OA_CLEARED"),
            ("APPLIED", "APPLIED"),  # raw enum typed directly over the dropdown
            ("", None),
            ("   ", None),
            ("Some Gibberish", None),
        ],
    )
    def test_reverse_mapping(self, display, expected):
        assert _my_status_to_enum(display) == expected


def _values_get_execute_mock(fake_service: MagicMock) -> MagicMock:
    return fake_service.spreadsheets().values().get().execute


class TestReadMyStatusMapFailurePropagates:
    """B2: a transient read failure must not be swallowed into {} — that
    would make the caller's clear-and-write wipe every user-set My Status."""

    def test_api_error_propagates(self):
        fake_service = MagicMock()
        execute = _values_get_execute_mock(fake_service)
        execute.side_effect = HttpError(resp=MagicMock(status=503), content=b"error")
        sync = GoogleSheetsSync(
            settings=MagicMock(google_sheet_id="sheet123"), service=fake_service
        )

        with pytest.raises(HttpError):
            sync._read_my_status_map()

    def test_empty_sheet_still_returns_blank_dict(self):
        fake_service = MagicMock()
        execute = _values_get_execute_mock(fake_service)
        execute.return_value = {"values": []}
        sync = GoogleSheetsSync(
            settings=MagicMock(google_sheet_id="sheet123"), service=fake_service
        )

        assert sync._read_my_status_map() == {}

    def test_reads_back_values_when_present(self):
        fake_service = MagicMock()
        execute = _values_get_execute_mock(fake_service)
        execute.return_value = {
            "values": [
                ["Company", "My Status", "Drive ID"],
                ["Microsoft", "Applied", "MICROSOFT_2027_SDE_INTERN"],
                ["Dell", "Not Applied", "DELL_2027_INTERN"],
            ]
        }
        sync = GoogleSheetsSync(
            settings=MagicMock(google_sheet_id="sheet123"), service=fake_service
        )

        result = sync._read_my_status_map()

        assert result == {"MICROSOFT_2027_SDE_INTERN": "Applied"}
