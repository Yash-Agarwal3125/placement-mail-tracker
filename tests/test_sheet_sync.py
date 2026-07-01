"""Google Sheets row-builder tests for the redesigned 5-tab layout.

Covers:
- ``opportunity_to_sheet_row`` (ALL DRIVES, 11 columns)
- ``action_required_row`` (ACTION REQUIRED, 7 columns)
- ``upcoming_event_row`` (UPCOMING EVENTS, 4 columns)
- ``company_to_sheet_row``
- Header counts and order
"""

from __future__ import annotations

from placement_mail_tracker.sheets.sheets_sync import (
    ACTION_REQUIRED_HEADERS,
    ACTIVE_OPP_HEADERS,
    ACTIVE_USER_COLUMNS,
    ALL_DRIVES_HEADERS,
    COMPANY_HISTORY_HEADERS,
    MY_APPLICATIONS_HEADERS,
    UPCOMING_EVENTS_HEADERS,
    _preserve_user_columns,
    action_required_row,
    company_to_sheet_row,
    my_applications_row,
    opportunity_to_sheet_row,
    upcoming_event_row,
)

# Column positions for ALL DRIVES
COL = {name: i for i, name in enumerate(ALL_DRIVES_HEADERS)}
# Column positions for ACTION REQUIRED
ACOL = {name: i for i, name in enumerate(ACTION_REQUIRED_HEADERS)}


def _sample_opp(**overrides) -> dict:
    base = {
        "company_name": "Microsoft",
        "role": "Software Engineer Intern",
        "internship_or_fulltime": "internship",
        "current_status": "OA",
        "priority": "HIGH",
        "action_required": "Prepare for OA",
        "deadline": "2027-06-15",
        "next_event_date": "2027-06-10",
        "oa_date": "2027-06-10",
        "package_or_stipend": "50000 per month",
        "work_location": "Bangalore",
        "cgpa_requirement": "7.0",
        "branches_allowed": ["CSE", "ECE"],
        "eligibility_status": "ELIGIBLE",
        "registration_link": "https://forms.gle/test123",
        "source_email_id": "msg_ms_001",
        "source_thread_id": "thread_ms_001",
        "drive_id": "MICROSOFT_2027_SDE_INTERN",
        "email_received_at": "2027-05-20",
        "updated_at": "2027-05-29T10:30:00+00:00",
        "degree_level": "BTECH",
    }
    base.update(overrides)
    return base


class TestAllDrivesRow:
    def test_column_count_is_11(self):
        row = opportunity_to_sheet_row(_sample_opp())
        assert len(row) == 11
        assert len(row) == len(ALL_DRIVES_HEADERS)

    def test_company_and_role(self):
        row = opportunity_to_sheet_row(_sample_opp())
        assert row[COL["Company"]] == "Microsoft"
        assert row[COL["Role"]] == "Software Engineer Intern"

    def test_status_is_friendly(self):
        row = opportunity_to_sheet_row(_sample_opp())
        assert row[COL["Current Status"]] == "OA Scheduled"

    def test_deadline_is_human_readable(self):
        row = opportunity_to_sheet_row(_sample_opp())
        assert "T" not in row[COL["Deadline"]]  # not raw ISO

    def test_package_and_location(self):
        row = opportunity_to_sheet_row(_sample_opp())
        assert row[COL["Package/Stipend"]] == "50000 per month"
        assert row[COL["Location"]] == "Bangalore"

    def test_eligibility_shows_branch_string(self):
        row = opportunity_to_sheet_row(_sample_opp())
        # format_eligibility_string returns "B.Tech - CSE, ECE"
        assert "CSE" in row[COL["Eligibility"]]

    def test_eligibility_fallback_when_no_branches(self):
        row = opportunity_to_sheet_row(_sample_opp(branches_allowed=[], degree_level="UNKNOWN"))
        # Falls back to eligibility_status label
        assert row[COL["Eligibility"]] == "Eligible"

    def test_last_update_is_human_not_iso(self):
        row = opportunity_to_sheet_row(_sample_opp())
        cell = row[COL["Last Update"]]
        assert "T" not in cell and "+00:00" not in cell
        assert "2027" in cell

    def test_received_date_formatted(self):
        row = opportunity_to_sheet_row(_sample_opp())
        cell = row[COL["Received Date"]]
        assert "2027" in cell


class TestActionRequiredRow:
    def test_column_count_is_8(self):
        row = action_required_row(_sample_opp())
        assert len(row) == 8
        assert len(row) == len(ACTION_REQUIRED_HEADERS)

    def test_company_and_status(self):
        row = action_required_row(_sample_opp())
        assert row[ACOL["Company"]] == "Microsoft"
        assert row[ACOL["Status"]] == "OA Scheduled"

    def test_next_action_cell(self):
        row = action_required_row(_sample_opp())
        assert row[ACOL["Next Action"]] == "Prepare for OA"

    def test_deadline_cell(self):
        row = action_required_row(_sample_opp())
        assert "2027" in row[ACOL["Deadline"]]

    def test_received_cell(self):
        row = action_required_row(_sample_opp())
        assert "2027" in row[ACOL["Received"]]


class TestUpcomingEventRow:
    def test_column_count_is_4(self):
        row = upcoming_event_row(_sample_opp(), "Online Assessment", "2027-06-10")
        assert len(row) == 4
        assert len(row) == len(UPCOMING_EVENTS_HEADERS)

    def test_event_type_and_company(self):
        row = upcoming_event_row(_sample_opp(), "Interview", "2027-06-20")
        assert row[1] == "Microsoft"
        assert row[2] == "Interview"

    def test_date_formatted(self):
        row = upcoming_event_row(_sample_opp(), "Online Assessment", "2027-06-10")
        assert "2027" in row[0]


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


class TestHeaders:
    def test_all_drives_header_count(self):
        assert len(ALL_DRIVES_HEADERS) == 11

    def test_active_opp_headers_alias(self):
        assert ACTIVE_OPP_HEADERS is ALL_DRIVES_HEADERS

    def test_action_required_header_count(self):
        assert len(ACTION_REQUIRED_HEADERS) == 8

    def test_upcoming_events_header_count(self):
        assert len(UPCOMING_EVENTS_HEADERS) == 4

    def test_company_headers_count(self):
        assert len(COMPANY_HISTORY_HEADERS) == 6

    def test_my_applications_header_count(self):
        assert len(MY_APPLICATIONS_HEADERS) == 7

    def test_headers_are_strings(self):
        for headers in (ALL_DRIVES_HEADERS, ACTION_REQUIRED_HEADERS, UPCOMING_EVENTS_HEADERS):
            assert all(isinstance(h, str) for h in headers)

    def test_active_user_columns_has_my_status(self):
        assert ACTIVE_USER_COLUMNS == [ALL_DRIVES_HEADERS.index("My Status")]

    def test_preserve_user_columns_noop(self):
        new_row = ["a", "b", "c"]
        _preserve_user_columns(new_row, ["x", "y", "z"], [])
        assert new_row == ["a", "b", "c"]

    def test_my_applications_row_structure(self):
        row = my_applications_row(_sample_opp(), "Applied")
        assert len(row) == 7
        assert row[0] == "Microsoft"
        assert row[2] == "Applied"


class _FakeValues:
    """Records update/clear call order and can fail a chosen operation."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on

    def update(self, **kwargs):
        return self._op("update", kwargs)

    def clear(self, **kwargs):
        return self._op("clear", kwargs)

    def _op(self, name: str, kwargs: dict):
        self.calls.append((name, kwargs.get("range", "")))
        outer = self

        class _Exec:
            def execute(self_inner):
                if outer.fail_on == name:
                    raise RuntimeError(f"{name} failed")
                return {}

        return _Exec()


class TestAtomicTabWrite:
    """T1.3: write-before-clear so a mid-sync failure never blanks a tab."""

    def _sync(self):
        import pytest

        from placement_mail_tracker.config.settings import Settings
        from placement_mail_tracker.sheets.sheets_sync import GoogleSheetsSync

        settings = Settings(app_env="testing", google_sheet_id="SHEET")
        return GoogleSheetsSync(settings, service=object()), pytest

    def test_update_happens_before_clear(self):
        sync, _ = self._sync()
        fake = _FakeValues()
        sync._values = lambda: fake
        sync._clear_and_write_tab("ALL DRIVES", ["A", "B"], [["1", "2"]])
        assert [c[0] for c in fake.calls] == ["update", "clear"]

    def test_update_failure_never_clears(self):
        sync, pytest = self._sync()
        fake = _FakeValues(fail_on="update")
        sync._values = lambda: fake
        with pytest.raises(RuntimeError):
            sync._clear_and_write_tab("ALL DRIVES", ["A"], [])
        # Nothing was cleared, so the previous tab contents remain intact.
        assert "clear" not in [c[0] for c in fake.calls]

    def test_trim_clears_only_rows_below_written_data(self):
        sync, _ = self._sync()
        fake = _FakeValues()
        sync._values = lambda: fake
        # 1 header + 2 rows = 3 rows written; trim must start at row 4.
        sync._clear_and_write_tab("ALL DRIVES", ["H1", "H2"], [["a", "b"], ["c", "d"]])
        clear_range = next(rng for op, rng in fake.calls if op == "clear")
        assert clear_range == "'ALL DRIVES'!A4:Z"
