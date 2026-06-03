"""Tests for Phase 2, 3, 6, 7: Eligibility Engine."""

import pytest
from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.extraction.eligibility import evaluate_eligibility

@pytest.fixture
def base_profile():
    return UserProfile(
        degree="B.Tech",
        branch="AI & ML",
        campus="Vellore",
        graduation_year=2027,
        cgpa=8.7
    )

def test_eligible_it_branch(base_profile):
    opp_data = {
        "eligibility": "B.Tech/BE",
        "branches_allowed": "CS, IT, AI & ML",
        "cgpa_requirement": "7.5 CGPA"
    }
    assert evaluate_eligibility(opp_data, base_profile) == "ELIGIBLE"

def test_eligible_data_science_branch(base_profile):
    opp_data = {
        "eligibility": "B.Tech only",
        "branches_allowed": "Data Science, AIML",
    }
    assert evaluate_eligibility(opp_data, base_profile) == "ELIGIBLE"

def test_not_eligible_mechanical_only(base_profile):
    opp_data = {
        "eligibility": "B.Tech",
        "branches_allowed": "Mechanical and Civil only",
    }
    assert evaluate_eligibility(opp_data, base_profile) == "NOT_ELIGIBLE_BRANCH"

def test_not_eligible_mba_only(base_profile):
    opp_data = {
        "eligibility": "MBA candidates",
        "branches_allowed": "Marketing, Finance",
    }
    assert evaluate_eligibility(opp_data, base_profile) == "NOT_ELIGIBLE_DEGREE"

def test_cgpa_above_threshold(base_profile):
    opp_data = {
        "eligibility": "B.Tech",
        "cgpa_requirement": "Minimum 8.0 CGPA"
    }
    assert evaluate_eligibility(opp_data, base_profile) == "ELIGIBLE"

def test_cgpa_below_threshold(base_profile):
    opp_data = {
        "eligibility": "B.Tech",
        "cgpa_requirement": "Strictly 9.0 CGPA and above"
    }
    assert evaluate_eligibility(opp_data, base_profile) == "NOT_ELIGIBLE_CGPA"

def test_manual_review_when_empty(base_profile):
    opp_data = {}
    assert evaluate_eligibility(opp_data, base_profile) == "MANUAL_REVIEW"
