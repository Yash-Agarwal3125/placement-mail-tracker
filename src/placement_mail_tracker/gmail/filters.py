"""Placement and internship email filtering helpers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from placement_mail_tracker.gmail.gmail_client import GmailEmail
from placement_mail_tracker.utils.trusted_senders import TrustedSenderManager

logger = logging.getLogger(__name__)

PLACEMENT_KEYWORDS: dict[str, int] = {
    "placement": 20,
    "internship": 20,
    "shortlist": 18,
    "interview": 18,
    "online assessment": 18,
    "oa": 12,
    "coding test": 18,
    "recruitment": 16,
    "hiring": 16,
    "campus drive": 20,
    "campus hiring": 20,
    "pre placement": 16,
    "pre-placement": 16,
    "job opportunity": 14,
    "career opportunity": 12,
}

PLACEMENT_SENDER_KEYWORDS: dict[str, int] = {
    "cdc": 30,
    "placement": 30,
    "placements": 30,
    "career development": 24,
    "training and placement": 28,
    "tpo": 24,
    "campus": 14,
}

NEWSLETTER_KEYWORDS = (
    "newsletter",
    "unsubscribe",
    "digest",
    "weekly roundup",
    "webinar",
    "bootcamp",
    "course",
    "sale",
    "discount",
    "promotion",
    "sponsored",
    "marketing",
)

IRRELEVANT_SENDERS = (
    "noreply@medium.com",
    "quora",
    "substack",
    "linkedin job alerts",
    "coursera",
    "udemy",
)

PLACEMENT_THRESHOLD = 45


@dataclass(slots=True)
class FilterDecision:
    """Structured metadata explaining a placement filtering decision."""

    is_placement: bool
    score: int
    confidence: str
    matched_keywords: list[str] = field(default_factory=list)
    matched_sender_terms: list[str] = field(default_factory=list)
    subject_matches: list[str] = field(default_factory=list)
    ignored_reasons: list[str] = field(default_factory=list)


def is_placement_mail(
    email: GmailEmail | None = None,
    *,
    subject: str = "",
    sender: str = "",
    body: str = "",
) -> FilterDecision:
    """Return a structured placement-mail filtering decision."""
    if email is not None:
        subject = email.subject
        sender = email.sender
        body = email.body_text

    decision = calculate_relevance_score(subject=subject, sender=sender, body=body)
    logger.debug(
        "Placement filter decision: is_placement=%s score=%s subject=%r sender=%r",
        decision.is_placement,
        decision.score,
        subject,
        sender,
    )
    return decision


def calculate_relevance_score(
    *,
    subject: str = "",
    sender: str = "",
    body: str = "",
) -> FilterDecision:
    """Score an email for placement or internship relevance with trusted sender discovery."""
    normalized_subject = _normalize(subject)
    normalized_sender = _normalize(sender)
    normalized_body = _normalize(body)
    combined_text = f"{normalized_subject} {normalized_body}"

    score = 0
    matched_keywords: list[str] = []
    matched_sender_terms: list[str] = []
    subject_matches: list[str] = []
    ignored_reasons: list[str] = []

    # 1. Dynamic Trusted Sender Discovery and Evaluation
    if sender:
        sender_manager = TrustedSenderManager()
        is_trusted, sender_score = sender_manager.process_and_discover(sender, subject)
        if is_trusted:
            matched_sender_terms.append(f"trusted_sender:{sender_score}")
            score += 55  # Exceeds PLACEMENT_THRESHOLD automatically

    for keyword, weight in PLACEMENT_KEYWORDS.items():
        if _contains_term(combined_text, keyword):
            matched_keywords.append(keyword)
            score += weight

        if _contains_term(normalized_subject, keyword):
            subject_matches.append(keyword)
            score += max(8, weight // 2)

    for sender_term, weight in PLACEMENT_SENDER_KEYWORDS.items():
        if _contains_term(normalized_sender, sender_term):
            matched_sender_terms.append(sender_term)
            score += weight

    newsletter_hits = [term for term in NEWSLETTER_KEYWORDS if _contains_term(combined_text, term)]
    irrelevant_sender_hits = [
        term for term in IRRELEVANT_SENDERS if _contains_term(normalized_sender, term)
    ]

    if newsletter_hits:
        ignored_reasons.append(f"newsletter_or_marketing_terms:{','.join(newsletter_hits)}")
        score -= 30

    if irrelevant_sender_hits:
        ignored_reasons.append(f"irrelevant_sender:{','.join(irrelevant_sender_hits)}")
        score -= 35

    if not matched_keywords and not matched_sender_terms:
        ignored_reasons.append("no_placement_signals")

    score = max(0, min(score, 100))
    is_placement = score >= PLACEMENT_THRESHOLD and not _is_strong_ignore(ignored_reasons, score)

    return FilterDecision(
        is_placement=is_placement,
        score=score,
        confidence=_confidence_label(score),
        matched_keywords=matched_keywords,
        matched_sender_terms=matched_sender_terms,
        subject_matches=subject_matches,
        ignored_reasons=ignored_reasons,
    )


def _normalize(value: str) -> str:
    """Normalize text for simple rule-based matching."""
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _contains_term(text: str, term: str) -> bool:
    """Match a keyword or phrase without partial-word false positives."""
    normalized_term = _normalize(term)
    if not normalized_term:
        return False

    if len(normalized_term) <= 3:
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])"
        return re.search(pattern, text) is not None

    return normalized_term in text


def _confidence_label(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= PLACEMENT_THRESHOLD:
        return "medium"
    if score >= 25:
        return "low"
    return "none"


def _is_strong_ignore(ignored_reasons: list[str], score: int) -> bool:
    """Avoid letting generic job-newsletter content pass as placement mail."""
    if score >= 75:
        return False
    ignored_prefixes = ("newsletter_or_marketing_terms", "irrelevant_sender")
    return any(reason.startswith(ignored_prefixes) for reason in ignored_reasons)
