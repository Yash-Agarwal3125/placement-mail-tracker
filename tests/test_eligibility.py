"""Tests for Phase 2, 3, 6, 7: Eligibility Engine."""

import pytest

from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.extraction.eligibility import (
    evaluate_eligibility,
    format_eligibility_string,
)


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


def test_mtech_text_signal_filtered_for_btech(base_profile):
    opp_data = {
        "eligibility": "M.Tech students only",
        "branches_allowed": "",
    }
    assert evaluate_eligibility(opp_data, base_profile) == "NOT_ELIGIBLE_DEGREE"


def test_mtech_with_btech_both_eligible(base_profile):
    opp_data = {
        "eligibility": "B.Tech and M.Tech students",
        "branches_allowed": "CSE",
    }
    # Should not be filtered — both degrees mentioned
    result = evaluate_eligibility(opp_data, base_profile)
    assert result in ("ELIGIBLE", "MANUAL_REVIEW")


# --- format_eligibility_string ---

def test_format_eligibility_btech_with_branches():
    opp_data = {"degree_level": "BTECH", "branches_allowed": ["CSE", "AI & ML"]}
    assert format_eligibility_string(opp_data) == "B.Tech - CSE, AI & ML"


def test_format_eligibility_any_no_branches():
    opp_data = {"degree_level": "ANY", "branches_allowed": []}
    assert format_eligibility_string(opp_data) == "Any"


def test_format_eligibility_unknown_no_branches():
    opp_data = {"degree_level": "UNKNOWN", "branches_allowed": []}
    assert format_eligibility_string(opp_data) == ""


def test_format_eligibility_json_string_branches():
    opp_data = {"degree_level": "BTECH", "branches_allowed": '["CSE", "ECE"]'}
    result = format_eligibility_string(opp_data)
    assert "CSE" in result and "ECE" in result
