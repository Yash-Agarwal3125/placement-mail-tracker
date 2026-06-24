"""Tests for Phase 8, 9: Smart Alerts and Notifications."""

from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.scheduler.alert_generator import AlertGenerator
from placement_mail_tracker.scheduler.digest_generator import _format_digest
from placement_mail_tracker.utils.scoring import compute_priority


@pytest.fixture
def mock_profile():
    return UserProfile(
        degree="B.Tech",
        branch="AI & ML",
        campus="Vellore",
        graduation_year=2027,
        cgpa=8.7
    )

def test_priority_scoring_not_eligible(mock_profile):
    opp = {
        "eligibility_status": "NOT_ELIGIBLE_DEGREE",
        "deadline": (datetime.now() + timedelta(hours=10)).isoformat()
    }
    assert compute_priority(opp, mock_profile) == "LOW"

def test_priority_scoring_high_status(mock_profile):
    opp = {
        "eligibility_status": "ELIGIBLE",
        "current_status": "INTERVIEW",
        "deadline": (datetime.now() + timedelta(days=5)).isoformat()
    }
    assert compute_priority(opp, mock_profile) == "HIGH"

def test_priority_scoring_deadline_proximity(mock_profile):
    opp = {
        "eligibility_status": "ELIGIBLE",
        "current_status": "OPEN",
        "deadline": (datetime.now() + timedelta(hours=24)).isoformat()
    }
    assert compute_priority(opp, mock_profile) == "HIGH"

def test_priority_scoring_medium(mock_profile):
    opp = {
        "eligibility_status": "ELIGIBLE",
        "current_status": "OPEN",
        "deadline": (datetime.now() + timedelta(days=10)).isoformat()
    }
    assert compute_priority(opp, mock_profile) == "MEDIUM"

def test_alert_generation_logic():
    # Setup mock
    mock_db = Mock(spec=DatabaseManager)
    mock_db.connection = Mock()
    mock_settings = Mock()
    mock_settings.smtp_email = "test@gmail.com"
    mock_settings.smtp_app_password = "password"
    mock_settings.notification_email = "notify@gmail.com"
    mock_settings.email_receiver = "recv@gmail.com"
    mock_notifier = Mock()
    
    alert_gen = AlertGenerator(mock_db, mock_settings)
    alert_gen.notifier = mock_notifier
    alert_gen._should_send_alert = Mock(return_value=True)
    alert_gen._mark_alert_sent = Mock()
    
    now = datetime.now()
    
    opp_deadline_4h = {
        "id": 1,
        "company_name": "Test Co",
        "eligibility_status": "ELIGIBLE",
        "deadline": (now + timedelta(hours=3)).isoformat()
    }
    
    alert_gen._check_deadline_alerts(opp_deadline_4h, now)
    
    # Assert email sent with DEADLINE_4H
    assert mock_notifier.send_email.call_count == 1
    call_args = mock_notifier.send_email.call_args[1]
    assert "Test Co" in call_args["subject"]
    assert "<3 hours" in call_args["subject"]
    
def test_digest_format():
    action_required = [
        {"company_name": "Action Co", "role": "Dev", "current_status": "OPEN", "deadline": None}
    ]
    upcoming_events = [
        {"company_name": "Event Co", "role": "Dev", "next_event_date": "2027-06-10"}
    ]
    new_opps = [{"company_name": "New Co", "role": "Dev"}]

    output = _format_digest(action_required, upcoming_events, new_opps, datetime.now())

    assert "PLACEMENT SUMMARY" in output
    assert "ACTION REQUIRED" in output
    assert "Action Co" in output
    assert "UPCOMING INTERVIEWS" in output
    assert "Event Co" in output
    assert "NEW OPPORTUNITIES" in output
    assert "New Co" in output
