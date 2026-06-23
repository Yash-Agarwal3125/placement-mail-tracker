"""Google Sheets row-builder tests for the human-readable Active Opportunities layout.

Covers:
- ``opportunity_to_sheet_row`` column count and order (20 columns)
- Human-readable dates, Days-Left countdown, friendly enum labels
- Apply-link and Gmail deep-link generation
- Compact status-history trail
- ``company_to_sheet_row``
"""

from __future__ import annotations

from datetime import datetime, timedelta

from placement_mail_tracker.sheets.sheets_sync import (
    ACTIVE_OPP_HEADERS,
    ACTIVE_USER_COLUMNS,
    COMPANY_HISTORY_HEADERS,
    _preserve_user_columns,
    company_to_sheet_row,
    opportunity_to_sheet_row,
)

# Column positions (kept in one place so the tests document the layout).
COL = {name: i for i, name in enumerate(ACTIVE_OPP_HEADERS)}


class TestActiveOppLayout:
    def _sample_opp(self, **overrides) -> dict:
        base = {
            "company_name": "Microsoft",
            "role": "Software Engineer Intern",
            "internship_or_fulltime": "internship",
            "current_status": "OA",
            "priority": "HIGH",
            "action_required": "PREPARE FOR TEST",
            "deadline": "2027-06-15",
            "next_event_date": "2027-06-10",
            "package_or_stipend": "50000 per month",
            "work_location": "Bangalore",
            "cgpa_requirement": "7.0",
            "branches_allowed": ["CSE", "ECE"],
            "eligibility_status": "ELIGIBLE",
            "my_status": "APPLIED",
            "registration_link": "https://forms.gle/test123",
            "status_history": '["OPEN", "OA"]',
            "source_email_id": "msg_ms_001",
            "source_thread_id": "thread_ms_001",
            "drive_id": "MICROSOFT_2027_SDE_INTERN",
            "updated_at": "2027-05-29T10:30:00+00:00",
        }
        base.update(overrides)
        return base

    def test_column_count_is_22(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert len(row) == 22
        assert len(row) == len(ACTIVE_OPP_HEADERS)

    def test_core_identity_columns(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert row[COL["Company"]] == "Microsoft"
        assert row[COL["Role"]] == "Software Engineer Intern"
        assert row[COL["Drive ID"]] == "MICROSOFT_2027_SDE_INTERN"

    def test_friendly_enum_labels(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert row[COL["Type"]] == "Internship"
        assert row[COL["Status"]] == "OA Scheduled"
        assert row[COL["Priority"]] == "High"
        assert row[COL["Eligibility"]] == "Eligible"
        assert row[COL["My Status"]] == "Applied"

    def test_human_readable_deadline_and_days_left(self):
        in_five_days = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        row = opportunity_to_sheet_row(self._sample_opp(deadline=in_five_days))
        assert "T" not in row[COL["Deadline"]]  # not raw ISO
        assert row[COL["Days Left"]] == "5 days"

    def test_days_left_special_words(self):
        def days_left(offset: int) -> str:
            when = (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")
            return opportunity_to_sheet_row(self._sample_opp(deadline=when))[COL["Days Left"]]

        assert days_left(0) == "Today"
        assert days_left(1) == "Tomorrow"
        assert days_left(-2) == "Passed"

    def test_last_updated_is_human_not_iso(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        cell = row[COL["Last Updated"]]
        assert "T" not in cell and "+00:00" not in cell
        assert "2027" in cell

    def test_package_cgpa_branches_location(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert row[COL["Package"]] == "50000 per month"
        assert row[COL["CGPA Cutoff"]] == "7.0"
        assert row[COL["Branches"]] == "CSE, ECE"
        assert row[COL["Location"]] == "Bangalore"

    def test_branches_junk_is_cleaned(self):
        row = opportunity_to_sheet_row(self._sample_opp(branches_allowed=["[]"]))
        assert row[COL["Branches"]] == ""

    def test_apply_link_hyperlink(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        cell = row[COL["Apply Link"]]
        assert "HYPERLINK" in cell and "forms.gle/test123" in cell

    def test_no_apply_link_when_missing(self):
        row = opportunity_to_sheet_row(self._sample_opp(registration_link=None))
        assert row[COL["Apply Link"]] == ""

    def test_gmail_link_from_thread(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        cell = row[COL["Email"]]
        assert "HYPERLINK" in cell and "thread_ms_001" in cell

    def test_gmail_link_falls_back_to_message_id(self):
        row = opportunity_to_sheet_row(self._sample_opp(source_thread_id=None))
        assert "msg_ms_001" in row[COL["Email"]]

    def test_no_gmail_link_when_no_ids(self):
        row = opportunity_to_sheet_row(
            self._sample_opp(source_thread_id=None, source_email_id=None)
        )
        assert row[COL["Email"]] == ""

    def test_status_history_is_compact_trail(self):
        row = opportunity_to_sheet_row(self._sample_opp())
        assert row[COL["History"]] == "Open → OA Scheduled"

    def test_my_status_defaults_to_not_applied(self):
        opp = self._sample_opp()
        del opp["my_status"]
        row = opportunity_to_sheet_row(opp)
        assert row[COL["My Status"]] == "Not applied"


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

    def test_company_row_with_none_values(self):
        row = company_to_sheet_row({"name": "Unknown"})
        assert row[0] == "Unknown"
        assert row[1] == ""


# ===================================================================
# Header validation
# ===================================================================


class TestUserColumnPreservation:
    """The sync must not overwrite columns the user edits by hand (My Status)."""

    def test_my_status_edit_is_preserved(self):
        my_status_col = ACTIVE_OPP_HEADERS.index("My Status")
        # Freshly built row carries the DB default "Not applied".
        new_row = ["x"] * len(ACTIVE_OPP_HEADERS)
        new_row[my_status_col] = "Not applied"
        # The sheet already has the user's edit.
        existing_row = ["x"] * len(ACTIVE_OPP_HEADERS)
        existing_row[my_status_col] = "Applied"

        _preserve_user_columns(new_row, existing_row, ACTIVE_USER_COLUMNS)
        assert new_row[my_status_col] == "Applied"

    def test_blank_existing_does_not_override(self):
        col = ACTIVE_OPP_HEADERS.index("My Status")
        new_row = ["x"] * len(ACTIVE_OPP_HEADERS)
        new_row[col] = "Not applied"
        existing_row = ["x"] * len(ACTIVE_OPP_HEADERS)
        existing_row[col] = ""  # user never touched it
        _preserve_user_columns(new_row, existing_row, ACTIVE_USER_COLUMNS)
        assert new_row[col] == "Not applied"


class TestHeaders:
    def test_active_opp_headers_count(self):
        assert len(ACTIVE_OPP_HEADERS) == 22

    def test_drive_id_is_last(self):
        assert ACTIVE_OPP_HEADERS[-1] == "Drive ID"

    def test_company_headers_count(self):
        assert len(COMPANY_HISTORY_HEADERS) == 6

    def test_headers_are_strings(self):
        assert all(isinstance(h, str) for h in ACTIVE_OPP_HEADERS)
