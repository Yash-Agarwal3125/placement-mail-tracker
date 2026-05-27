"""Tests for Google Sheets synchronization helpers."""

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.sheets.sheets_sync import (
    GoogleSheetsSync,
    build_company_role_key,
    build_existing_row_index,
    build_sync_key,
    opportunity_to_sheet_row,
    quote_sheet_name,
)


class FakeRequest:
    def __init__(self, response=None, callback=None) -> None:
        self.response = response or {}
        self.callback = callback

    def execute(self):
        if self.callback:
            return self.callback()
        return self.response


class FakeValues:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.updates = []
        self.appends = []

    def get(self, **kwargs):
        if kwargs["range"].endswith("A1:U1"):
            values = [self.rows[0]] if self.rows else []
            return FakeRequest({"values": values})
        return FakeRequest({"values": self.rows})

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return FakeRequest({})

    def append(self, **kwargs):
        self.appends.append(kwargs)
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self, values) -> None:
        self._values = values

    def values(self):
        return self._values


class FakeService:
    def __init__(self, values) -> None:
        self._values = values

    def spreadsheets(self):
        return FakeSpreadsheets(self._values)


def test_opportunity_to_sheet_row_contains_stable_key_and_lists() -> None:
    row = opportunity_to_sheet_row(
        {
            "id": 7,
            "company_name": "ExampleTech",
            "role": "SDE Intern",
            "branches_allowed": ["CSE", "IT"],
            "hiring_process": ["OA", "Interview"],
            "important_notes": ["Carry ID card"],
        }
    )

    assert row[0] == "opportunity:7"
    assert row[2] == "ExampleTech"
    assert row[8] == "CSE, IT"
    assert row[14] == "OA, Interview"
    assert row[15] == "Carry ID card"


def test_build_existing_row_index_uses_sync_key_and_fallback() -> None:
    rows = [
        ["sync_key", "opportunity_id", "company_name", "role"],
        ["opportunity:1", "1", "ExampleTech", "SDE"],
        ["", "", "Acme Corp", "Analyst Intern"],
    ]

    index = build_existing_row_index(rows)

    assert index["opportunity:1"] == 2
    assert index[build_company_role_key("Acme Corp", "Analyst Intern")] == 3


def test_sync_opportunities_updates_existing_and_appends_new() -> None:
    existing_row = opportunity_to_sheet_row(
        {"id": 1, "company_name": "ExampleTech", "role": "SDE Intern"}
    )
    values = FakeValues(rows=[["wrong"], existing_row])
    service = FakeService(values)
    settings = Settings(GOOGLE_SHEET_ID="sheet-id")
    sync = GoogleSheetsSync(settings, service=service)

    result = sync.sync_opportunities(
        [
            {"id": 1, "company_name": "ExampleTech", "role": "SDE Intern"},
            {"id": 2, "company_name": "Acme", "role": "Data Intern"},
        ]
    )

    assert result == {"created": 1, "updated": 1, "skipped": 0}
    assert len(values.updates) == 2
    assert values.updates[0]["range"] == "'Opportunities'!A1:U1"
    assert values.updates[1]["range"] == "'Opportunities'!A2:U2"
    assert len(values.appends) == 1
    assert values.appends[0]["body"]["values"][0][0] == "opportunity:2"


def test_sync_skips_when_sheet_id_missing(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    sync = GoogleSheetsSync(Settings(GOOGLE_SHEET_ID=""), service=FakeService(FakeValues()))

    result = sync.sync_opportunities([{"id": 1, "company_name": "A", "role": "B"}])

    assert result == {"created": 0, "updated": 0, "skipped": 1}


def test_quote_sheet_name_and_sync_key_helpers() -> None:
    assert quote_sheet_name("My Sheet") == "'My Sheet'"
    assert quote_sheet_name("Team's Sheet") == "'Team''s Sheet'"
    assert build_sync_key({"company_name": "Acme Corp", "role": "SDE Intern"}).startswith(
        "company-role:"
    )
