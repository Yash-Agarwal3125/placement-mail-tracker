"""Tests for Phase 8: priority scoring."""

from datetime import datetime, timedelta

import pytest

from placement_mail_tracker.config.user_profile import UserProfile
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

