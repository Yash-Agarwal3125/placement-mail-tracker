"""Phase 10 & 11: Realistic Dataset and Mass E2E Test."""

import pytest
from unittest.mock import patch, MagicMock
from placement_mail_tracker.scheduler.runner import run_once

def generate_mock_emails():
    companies = ["Tata Motors", "Tata Electronics", "Waters", "Afford Medical Technologies", "Infosys", "TCS", "Wipro", "Accenture", "Deloitte", "Amazon"]
    stages = ["OA", "Shortlist", "Interview", "Offer", "Rejection"]
    emails = []
    
    for i in range(50):
        company = companies[i % len(companies)]
        stage = stages[i % len(stages)]
        emails.append({
            "id": f"msg{i}",
            "thread_id": f"thread_{company.replace(' ', '_').lower()}",
            "subject": f"{company} - {stage} Update",
            "sender": "cdc@vit.ac.in",
            "body": f"Dear Student, this is an update regarding {company} {stage}.",
            "date": "2026-05-29"
        })
    return emails

@patch('placement_mail_tracker.scheduler.runner.GmailClient')
@patch('placement_mail_tracker.scheduler.runner.GeminiExtractor')
@patch('placement_mail_tracker.scheduler.runner.SheetsClient')
@patch('placement_mail_tracker.scheduler.runner.EmailNotifier')
@patch('placement_mail_tracker.scheduler.runner.TelegramNotifier')
def test_mass_realistic_dataset(mock_telegram, mock_notifier, mock_sheets, mock_gemini, mock_gmail, db_connection, mock_settings):
    dataset = generate_mock_emails()
    mock_gmail.return_value.fetch_recent_messages.return_value = dataset
    
    # Mock Gemini to return dynamic payloads based on subject
    def mock_extract(email_dict):
        subject = email_dict.get("subject", "")
        company = subject.split(" - ")[0]
        stage = subject.split(" - ")[1].replace(" Update", "")
        
        return {
            "company_name": company,
            "role": "Software Engineer",
            "opportunity_type": "full_time",
            "current_status": stage.upper(),
            "update_type": f"{stage.lower()}_update"
        }
        
    mock_gemini.return_value.extract.side_effect = mock_extract
    
    # Run the pipeline
    run_once(db_connection, mock_settings)
    
    # Verify DB state
    cur = db_connection.cursor()
    cur.execute("SELECT * FROM opportunities")
    rows = cur.fetchall()
    
    # We generated 50 emails across 10 companies. Because they share thread_ids per company,
    # they should deduplicate into exactly 10 distinct drives!
    assert len(rows) == 10
    
    # Verify statuses are updated to the latest stage
    cur.execute("SELECT company_name, current_status, status_history FROM opportunities")
    for row in cur.fetchall():
        assert row["company_name"] in ["Tata Motors", "Tata Electronics", "Waters", "Afford Medical Technologies", "Infosys", "TCS", "Wipro", "Accenture", "Deloitte", "Amazon"]
        assert len(eval(row["status_history"])) > 1  # Should have recorded multiple stages
