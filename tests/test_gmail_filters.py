"""Tests for placement email filtering."""

from placement_mail_tracker.gmail.filters import calculate_relevance_score, is_placement_mail
from placement_mail_tracker.gmail.gmail_client import GmailEmail


def test_cdc_sender_with_internship_subject_is_placement_mail() -> None:
    decision = calculate_relevance_score(
        subject="Summer internship shortlist and interview schedule",
        sender="CDC Placements <cdc@college.edu>",
        body="You have been shortlisted for the next interview round.",
    )

    assert decision.is_placement is True
    assert decision.confidence == "high"
    assert "internship" in decision.matched_keywords
    assert "cdc" in decision.matched_sender_terms
    assert "shortlist" in decision.subject_matches


def test_online_assessment_subject_scores_as_relevant() -> None:
    decision = is_placement_mail(
        subject="Campus drive online assessment for Software Engineer",
        sender="placements@college.edu",
        body="The OA and coding test links will be shared tomorrow.",
    )

    assert decision.is_placement is True
    assert decision.score >= 45
    assert "online assessment" in decision.matched_keywords
    assert "coding test" in decision.matched_keywords


def test_newsletter_is_ignored_even_with_hiring_term() -> None:
    decision = calculate_relevance_score(
        subject="Weekly hiring newsletter",
        sender="newsletter@example.com",
        body="Sponsored jobs, webinar invites, and unsubscribe link.",
    )

    assert decision.is_placement is False
    assert decision.ignored_reasons


def test_gmail_email_object_can_be_filtered() -> None:
    email = GmailEmail(
        message_id="1",
        thread_id="t1",
        subject="Recruitment update: interview round",
        sender="CDC Office <cdc@college.edu>",
        timestamp="2026-05-26T12:00:00+05:30",
        body_text="Campus recruitment interview details are attached.",
        snippet="Recruitment update",
    )

    decision = is_placement_mail(email)

    assert decision.is_placement is True
    assert "recruitment" in decision.matched_keywords
