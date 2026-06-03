"""Phase 6, 7, 8: Google Sheets sync tests.

Covers:
- ``opportunity_to_sheet_row`` column count (18 columns)
- Gmail deep link generation (Phase 7)
- ``company_to_sheet_row``
- CTC vs Stipend column split (internship → stipend col, full_time → CTC col)
"""

from __future__ import annotations

import pytest

from placement_mail_tracker.sheets.sheets_sync import (
    ACTIVE_OPP_HEADERS,
    COMPANY_HISTORY_HEADERS,
    company_to_sheet_row,
    opportunity_to_sheet_row,
)


# ===================================================================
# opportunity_to_sheet_row
# ===================================================================


class TestOpportunityToSheetRow:
    """Validate the row builder for the Active Opportunities sheet."""

    def _sample_opp(self, **overrides) -> dict:
        base = {
            "email_received_at": "29-May-2027 10:30 AM",
            "company_name": "Microsoft",
            "drive_id": "MICROSOFT_2027_SDE_INTERN",
            "role": "Software Engineer Intern",
            "internship_or_fulltime": "internship",
            "current_status": "OA",
            "status_history": '["OPEN", "OA"]',
            "package_or_stipend": "50000 per month",
            "work_location": "Bangalore",
            "deadline": "15 June 2027",
            "next_event_date": "10 June 2027",
            "action_required": "PREPARE FOR TEST",
            "my_status": "APPLIED",
            "last_update_timestamp": "2027-05-29T10:30:00+00:00",
            "source_email_id": "msg_ms_001",
            "source_thread_id": "thread_ms_001",
            "updated_at": "2027-05-29T10:30:00+00:00",
        }
        base.update(overrides)
        return base

    def test_column_count_is_18(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert len(row) == 19, f"Expected 19 columns, got {len(row)}"
        assert len(row) == len(ACTIVE_OPP_HEADERS)

    def test_gmail_link_generated_from_thread_id(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        email_col = row[17]  # "Open Email" column
        assert "HYPERLINK" in email_col
        assert "thread_ms_001" in email_col
        assert "mail.google.com" in email_col

    def test_gmail_link_from_message_id_when_no_thread(self):
        opp = self._sample_opp(source_thread_id=None)
        row = opportunity_to_sheet_row(opp)
        email_col = row[17]
        assert "msg_ms_001" in email_col

    def test_no_link_when_no_ids(self):
        opp = self._sample_opp(source_email_id=None, source_thread_id=None)
        row = opportunity_to_sheet_row(opp)
        assert row[17] == ""

    def test_company_name_in_row(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert row[1] == "Microsoft"

    def test_drive_id_in_row(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert row[2] == "MICROSOFT_2027_SDE_INTERN"

    def test_current_status_in_row(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert row[5] == "OA"

    def test_my_status_defaults_to_not_applied(self):
        opp = self._sample_opp()
        del opp["my_status"]
        row = opportunity_to_sheet_row(opp)
        assert row[14] == "NOT_APPLIED"


# ===================================================================
# CTC vs Stipend column split
# ===================================================================


class TestCtcVsStipendSplit:
    """Verify that package_or_stipend is routed to the correct column."""

    def test_internship_goes_to_stipend_column(self):
        opp = {
            "internship_or_fulltime": "internship",
            "package_or_stipend": "50000 per month",
        }
        row = opportunity_to_sheet_row(opp)
        ctc_col = row[7]    # CTC
        stipend_col = row[8]  # Stipend
        assert stipend_col == "50000 per month"
        assert ctc_col == ""

    def test_fulltime_goes_to_ctc_column(self):
        opp = {
            "internship_or_fulltime": "full_time",
            "package_or_stipend": "12 LPA",
        }
        row = opportunity_to_sheet_row(opp)
        ctc_col = row[7]
        stipend_col = row[8]
        assert ctc_col == "12 LPA"
        assert stipend_col == ""

    def test_unknown_category_defaults_to_ctc(self):
        opp = {
            "internship_or_fulltime": "",
            "package_or_stipend": "8 LPA",
        }
        row = opportunity_to_sheet_row(opp)
        assert row[7] == "8 LPA"  # CTC column
        assert row[8] == ""


# ===================================================================
# company_to_sheet_row
# ===================================================================


class TestCompanyToSheetRow:
    def test_company_row_structure(self):
        company = {
            "name": "Microsoft",
            "total_drives": 5,
            "selected_drives": 2,
            "rejected_drives": 1,
            "active_drives": 2,
            "last_activity": "2027-05-29T10:00:00+00:00",
        }
        row = company_to_sheet_row(company)
        assert len(row) == len(COMPANY_HISTORY_HEADERS)
        assert row[0] == "Microsoft"
        assert row[1] == "5"
        assert row[2] == "2"
        assert row[3] == "1"
        assert row[4] == "2"

    def test_company_row_with_none_values(self):
        company = {"name": "Unknown"}
        row = company_to_sheet_row(company)
        assert row[0] == "Unknown"
        # Missing fields should be empty strings
        assert row[1] == ""


# ===================================================================
# Header validation
# ===================================================================


class TestHeaders:
    def test_active_opp_headers_count(self):
        assert len(ACTIVE_OPP_HEADERS) == 19

    def test_company_headers_count(self):
        assert len(COMPANY_HISTORY_HEADERS) == 6

    def test_headers_are_strings(self):
        for h in ACTIVE_OPP_HEADERS:
            assert isinstance(h, str)
