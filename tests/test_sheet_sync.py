"""Phase 8: Google Sheets Tests."""

from placement_mail_tracker.sheets.sheets_sync import opportunity_to_sheet_row

def test_row_formatting():
    opp = {
        "email_received_at": "29-May-2026 10:00 AM",
        "company_name": "Amazon",
        "drive_id": "AMAZON_2026_01",
        "role": "SDE",
        "current_status": "OA",
        "status_history": ["NEW", "OA"],
        "package_or_stipend": "40 LPA",
        "source_message_id": "msg-123"
    }
    
    row = opportunity_to_sheet_row(opp)
    assert row[1] == "Amazon"
    assert row[2] == "AMAZON_2026_01"
    assert row[4] == "OA"
    assert "mail.google.com" in row[13]
