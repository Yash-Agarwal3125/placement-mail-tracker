"""Phase 3: Email filter tests for placement relevance scoring.

Validates that the ``is_placement_mail`` filter correctly accepts genuine CDC
placement emails and rejects irrelevant college-event/club emails.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from placement_mail_tracker.gmail.filters import (
    FilterDecision,
    is_placement_mail,
)


# ---------------------------------------------------------------------------
# Valid placement email fixtures
# ---------------------------------------------------------------------------

VALID_PLACEMENT_EMAILS = [
    pytest.param(
        "OA Scheduled – Microsoft Hiring 2027",
        "CDC VIT <cdc@vit.ac.in>",
        "Dear students, Online Assessment for Microsoft has been scheduled for 10 June 2027. "
        "Please register before the deadline. Placement cell, VIT.",
        id="oa_scheduled",
    ),
    pytest.param(
        "Interview Scheduled – Dell Technologies Campus Drive",
        "CDC VIT <placements@vit.ac.in>",
        "Students shortlisted for Dell Technologies interview round are requested to report "
        "on 15 June 2027 at SJT 301. Carry your resume and laptop.",
        id="interview_scheduled",
    ),
    pytest.param(
        "Shortlist – Standard Chartered Internship 2027",
        "CDC VIT <cdc@vit.ac.in>",
        "The following students have been shortlisted for the next round of Standard Chartered "
        "campus hiring process. Please check the attached list.",
        id="shortlist",
    ),
    pytest.param(
        "Offer Letter Released – HPE Campus Placement",
        "CDC VIT <cdc@vit.ac.in>",
        "Congratulations! The offer letters for Hewlett Packard Enterprise have been released. "
        "Selected candidates can collect them from the placement office.",
        id="offer_released",
    ),
    pytest.param(
        "PPT Announcement – Tata Motors Campus Drive",
        "Placements VIT <placements@vit.ac.in>",
        "Pre Placement Talk for Tata Motors has been scheduled on 5 June 2027 in the auditorium. "
        "All eligible students are invited to attend.",
        id="ppt_announcement",
    ),
    pytest.param(
        "Registration Open – Amazon Summer Internship 2027",
        "VIT CDC <vitianscdc@vit.ac.in>",
        "Registration for Amazon Summer Internship is now open. Eligible branches: CSE, ECE. "
        "Deadline: 20 June 2027. Register at https://forms.gle/xyz",
        id="registration_open",
    ),
]


@pytest.mark.parametrize("subject,sender,body", VALID_PLACEMENT_EMAILS)
def test_valid_placement_emails(subject: str, sender: str, body: str):
    """Genuine placement emails must be classified as relevant."""
    decision = is_placement_mail(subject=subject, sender=sender, body=body)
    assert decision.is_placement is True, (
        f"Expected placement=True for {subject!r}, got score={decision.score}, "
        f"reasons={decision.ignored_reasons}"
    )
    assert decision.score > 0


# ---------------------------------------------------------------------------
# Invalid / irrelevant email fixtures
# ---------------------------------------------------------------------------

INVALID_EMAILS = [
    pytest.param(
        "Club Meeting – IEEE VIT Chapter",
        "IEEE VIT <ieee@vitstudent.ac.in>",
        "Join us for the weekly IEEE VIT chapter meeting this Friday. Club activities include "
        "workshops and hackathon planning.",
        id="club_email",
    ),
    pytest.param(
        "Gravitas 2027 – Event Registration",
        "Gravitas Team <gravitas@vit.ac.in>",
        "Register now for Gravitas 2027! VIT's annual technical festival features events, "
        "competitions, and workshops. Event registration closes 1 Sept.",
        id="gravitas_event",
    ),
    pytest.param(
        "Workshop on Machine Learning – DSC VIT",
        "DSC VIT <dscvit@gmail.com>",
        "Attend our free workshop on Machine Learning fundamentals this weekend. "
        "Workshop registration link: https://example.com",
        id="workshop_email",
    ),
    pytest.param(
        "NPTEL Course Registration – Data Structures",
        "NPTEL Coordinator <nptel@vit.ac.in>",
        "Dear students, please complete your NPTEL registration for the course on "
        "Data Structures and Algorithms. NPTEL exam dates will be announced.",
        id="nptel_course",
    ),
    pytest.param(
        "Attendance Warning – Academic Notice",
        "Academic Office <academic@vit.ac.in>",
        "Your attendance in the current semester is below the minimum requirement. "
        "Academic notice: attendance must be above 75%.",
        id="attendance_notice",
    ),
    pytest.param(
        "Riviera 2027 – Cultural Event Registration",
        "Riviera Team <riviera@vit.ac.in>",
        "Register for Riviera 2027, VIT's annual cultural festival. Event registration "
        "deadline is approaching. Don't miss the fun!",
        id="cultural_event",
    ),
]


@pytest.mark.parametrize("subject,sender,body", INVALID_EMAILS)
def test_invalid_placement_emails(subject: str, sender: str, body: str):
    """Non-placement emails must be classified as irrelevant."""
    decision = is_placement_mail(subject=subject, sender=sender, body=body)
    assert decision.is_placement is False, (
        f"Expected placement=False for {subject!r}, got score={decision.score}, "
        f"keywords={decision.matched_keywords}"
    )


# ---------------------------------------------------------------------------
# Boundary / Confidence tests
# ---------------------------------------------------------------------------


def test_filter_returns_filter_decision_type():
    """is_placement_mail must always return a FilterDecision dataclass."""
    result = is_placement_mail(subject="Random email", sender="nobody@example.com", body="hello")
    assert isinstance(result, FilterDecision)
    assert hasattr(result, "is_placement")
    assert hasattr(result, "score")
    assert hasattr(result, "confidence")


def test_empty_email():
    """An empty email should not crash and should be irrelevant."""
    decision = is_placement_mail(subject="", sender="", body="")
    assert decision.is_placement is False
    assert decision.score == 0
