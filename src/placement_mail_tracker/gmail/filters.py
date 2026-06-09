"""Placement and internship email filtering helpers with relaxed debugging logic."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from email.utils import parseaddr

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

NEGATIVE_KEYWORDS = (
    "club",
    "committee",
    "student organization",
    "event registration",
    "workshop",
    "nptel",
    "academic notice",
    "gravitas",
    "riviera",
    "chapter",
    "fat schedule",
    "cat schedule",
    "exam schedule",
    "patents granted",
    "guest lecture",
    "blood donation",
    "hostel",
    "journal publication",
    "research paper",
    "merchandise",
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
    return decision


def calculate_relevance_score(
    *,
    subject: str = "",
    sender: str = "",
    body: str = "",
) -> FilterDecision:
    """Score an email for placement or internship relevance with relaxed discovery rules."""
    normalized_subject = _normalize(subject)
    normalized_sender = _normalize(sender)
    normalized_body = _normalize(body)
    combined_text = f"{normalized_subject} {normalized_body}"

    # Parse display name and email address
    display_name, email_address = parseaddr(sender)
    display_name_clean = display_name.strip()
    email_clean = email_address.lower().strip()
    domain = email_clean.split("@")[-1] if "@" in email_clean else ""

    subj_lower = subject.lower()
    disp_lower = display_name_clean.lower()
    email_lower = email_clean.lower()

    # 1. Evaluate Trusted Sender score (additive signal)
    sender_manager = TrustedSenderManager()
    is_trusted, sender_score = sender_manager.process_and_discover(sender, subject)

    # 2. Original scoring logic for unit tests compatibility
    classic_score = 0
    classic_matched_keywords: list[str] = []
    classic_matched_sender_terms: list[str] = []
    classic_subject_matches: list[str] = []
    classic_ignored_reasons: list[str] = []

    if is_trusted:
        classic_matched_sender_terms.append(f"trusted_sender:{sender_score}")
        classic_score += 55

    for keyword, weight in PLACEMENT_KEYWORDS.items():
        if _contains_term(combined_text, keyword):
            classic_matched_keywords.append(keyword)
            classic_score += weight
        if _contains_term(normalized_subject, keyword):
            classic_subject_matches.append(keyword)
            classic_score += max(8, weight // 2)

    for sender_term, weight in PLACEMENT_SENDER_KEYWORDS.items():
        if _contains_term(normalized_sender, sender_term):
            classic_matched_sender_terms.append(sender_term)
            classic_score += weight

    newsletter_hits = [term for term in NEWSLETTER_KEYWORDS if _contains_term(combined_text, term)]
    irrelevant_sender_hits = [
        term for term in IRRELEVANT_SENDERS if _contains_term(normalized_sender, term)
    ]
    negative_hits = [term for term in NEGATIVE_KEYWORDS if _contains_term(combined_text, term)]

    if newsletter_hits:
        classic_ignored_reasons.append(f"newsletter_or_marketing_terms:{','.join(newsletter_hits)}")
        classic_score -= 30

    if irrelevant_sender_hits:
        classic_ignored_reasons.append(f"irrelevant_sender:{','.join(irrelevant_sender_hits)}")
        classic_score -= 35

    if negative_hits:
        classic_ignored_reasons.append(f"negative_terms:{','.join(negative_hits)}")
        classic_score -= 50

    if not classic_matched_keywords and not classic_matched_sender_terms:
        classic_ignored_reasons.append("no_placement_signals")

    classic_score = max(0, min(classic_score, 100))
    classic_is_placement = classic_score >= PLACEMENT_THRESHOLD and not _is_strong_ignore(
        classic_ignored_reasons, classic_score
    )

    # 3. Relaxed matching rules (Task 2)
    # Rule A: Subject contains placement/internship keywords
    relaxed_subj_keywords = [
        "placement",
        "internship",
        "interview",
        "shortlist",
        "oa",
        "hiring",
        "online test",
        "registration",
    ]
    matched_subj_kws = []
    for kw in relaxed_subj_keywords:
        if kw == "oa":
            if re.search(r"\boa\b", subj_lower):
                matched_subj_kws.append(kw)
        elif kw in subj_lower:
            matched_subj_kws.append(kw)

    passed_relaxed_subject = len(matched_subj_kws) > 0

    # Rule B: Sender/display name contains sender keywords
    relaxed_sender_keywords = ["cdc", "placement", "career", "vitianscdc", "training"]
    matched_sender_kws = []
    for kw in relaxed_sender_keywords:
        if kw in disp_lower or kw in email_lower:
            matched_sender_kws.append(kw)

    passed_relaxed_sender = len(matched_sender_kws) > 0

    # Rule C: Sender domain matches institutional domains
    relaxed_domains = ["vit.ac.in", "vitstudent.ac.in"]
    matched_domains = [dom for dom in relaxed_domains if dom in domain]

    # Final combined filter decision
    is_placement = classic_is_placement or (
        (passed_relaxed_subject or passed_relaxed_sender)
        and not newsletter_hits
        and not irrelevant_sender_hits
        and not negative_hits
    )

    # Output detailed logs for Task 1 at INFO level (so they are visible by default)
    logger.info("==========================================")
    logger.info("[DEBUG] Evaluating email")
    logger.info("[DEBUG] Subject: %s", subject)
    logger.info("[DEBUG] Sender: %s", sender)
    logger.info("[DEBUG] Display Name: %s", display_name_clean)
    logger.info("[DEBUG] Email Address: %s", email_clean)
    logger.info("[DEBUG] Sender score: %s (Is Trusted: %s)", sender_score, is_trusted)
    logger.info("[DEBUG] Subject keyword matches: %s", matched_subj_kws or classic_subject_matches)
    logger.info(
        "[DEBUG] Sender keyword matches: %s", matched_sender_kws or classic_matched_sender_terms
    )
    logger.info("[DEBUG] Domain matches: %s", matched_domains)
    logger.info("[DEBUG] Final relevance decision: %s", "TRUE" if is_placement else "FALSE")

    rejection_reasons = []
    if not is_placement:
        if newsletter_hits:
            rejection_reasons.append(f"newsletter terms found: {newsletter_hits}")
        if irrelevant_sender_hits:
            rejection_reasons.append(f"irrelevant sender found: {irrelevant_sender_hits}")
        if negative_hits:
            rejection_reasons.append(f"negative terms found: {negative_hits}")
        if not (passed_relaxed_subject or passed_relaxed_sender):
            rejection_reasons.append("does not match relaxed subject or sender name filters")
        logger.info("[DEBUG] Rejection reason: %s", ", ".join(rejection_reasons))
    logger.info("==========================================")

    # Use combined metadata
    return FilterDecision(
        is_placement=is_placement,
        score=max(classic_score, 80 if is_placement else 0),
        confidence="high" if is_placement else "none",
        matched_keywords=list(set(classic_matched_keywords + matched_subj_kws)),
        matched_sender_terms=list(set(classic_matched_sender_terms + matched_sender_kws)),
        subject_matches=list(set(classic_subject_matches + matched_subj_kws)),
        ignored_reasons=rejection_reasons or classic_ignored_reasons,
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
    ignored_prefixes = ("newsletter_or_marketing_terms", "irrelevant_sender", "negative_terms")
    return any(reason.startswith(ignored_prefixes) for reason in ignored_reasons)
