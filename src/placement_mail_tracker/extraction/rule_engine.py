"""Phase 3: Rule-based extraction engine for placement emails.

Extracts company names, CTC, stipend, deadlines, locations, roles,
and status updates from email text using regex and keyword matching.
Only falls back to Gemini when critical fields are missing.

Phase 4: Company name normalization.
Phase 13: Email classification.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase 4: Company Normalization
# ---------------------------------------------------------------------------

# Common legal-entity suffixes and filler words to strip
_STRIP_SUFFIXES = re.compile(
    r"\b(inc\.?|ltd\.?|llc|llp|pvt\.?|private|limited|"
    r"technologies|technology|tech|solutions|services|systems|"
    r"global|india|corp\.?|corporation|group|holdings|enterprises|"
    r"co\.?|company)\b",
    re.IGNORECASE,
)

# Known canonical company names
_CANONICAL_NAMES: dict[str, str] = {
    "dell": "Dell Technologies",
    "microsoft": "Microsoft",
    "hpe": "Hewlett Packard Enterprise",
    "hewlett packard enterprise": "Hewlett Packard Enterprise",
    "hp": "HP",
    "tcs": "TCS",
    "tata consultancy services": "TCS",
    "infosys": "Infosys",
    "wipro": "Wipro",
    "accenture": "Accenture",
    "deloitte": "Deloitte",
    "amazon": "Amazon",
    "google": "Google",
    "meta": "Meta",
    "facebook": "Meta",
    "standard chartered": "Standard Chartered",
    "standardchartered": "Standard Chartered",
    "goldman sachs": "Goldman Sachs",
    "jp morgan": "JP Morgan",
    "jpmorgan": "JP Morgan",
    "tata motors": "Tata Motors",
    "tata electronics": "Tata Electronics",
    "afford medical": "Afford Medical Technologies",
    "waters": "Waters",
}


def normalize_company_name(raw: str | None) -> str:
    """Normalize a company name to a canonical form.

    Examples
    --------
    >>> normalize_company_name("Dell Technologies")
    'Dell Technologies'
    >>> normalize_company_name("DELL TECHNOLOGIES")
    'Dell Technologies'
    >>> normalize_company_name("Updated : Dell Technologies")
    'Dell Technologies'
    >>> normalize_company_name("Microsoft Corporation")
    'Microsoft'
    """
    if not raw:
        return ""

    # Strip common prefixes like "Updated :", "Reminder :", "Re:"
    cleaned = re.sub(
        r"^(updated\s*[:\-]|reminder\s*[:\-]|re\s*[:\-]|fwd?\s*[:\-])\s*",
        "",
        raw.strip(),
        flags=re.IGNORECASE,
    )

    # Strip trailing/leading whitespace and collapse spaces
    cleaned = " ".join(cleaned.split())

    # Check canonical mapping (case-insensitive)
    lookup_key = cleaned.casefold().strip()
    if lookup_key in _CANONICAL_NAMES:
        return _CANONICAL_NAMES[lookup_key]

    # Strip legal suffixes
    stripped = _STRIP_SUFFIXES.sub("", cleaned).strip()
    stripped = " ".join(stripped.split())  # collapse spaces after removal

    # Check canonical again after stripping
    lookup_key = stripped.casefold().strip()
    if lookup_key in _CANONICAL_NAMES:
        return _CANONICAL_NAMES[lookup_key]

    # Title-case the result
    if stripped:
        return stripped.title()
    return cleaned.title()


# ---------------------------------------------------------------------------
# Phase 13: Email Classification
# ---------------------------------------------------------------------------

EMAIL_CLASSIFICATIONS = (
    "NEW_DRIVE",
    "DRIVE_UPDATE",
    "OA_UPDATE",
    "SHORTLIST_UPDATE",
    "INTERVIEW_UPDATE",
    "OFFER_UPDATE",
    "REMINDER",
    "IRRELEVANT",
)

_CLASSIFICATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OA_UPDATE", re.compile(
        r"(online\s*(assessment|test)|oa\s*(scheduled|date|link|update)|"
        r"hackerrank|coding\s*test|assessment\s*(scheduled|link))",
        re.IGNORECASE,
    )),
    ("SHORTLIST_UPDATE", re.compile(
        r"(shortlist|short\s*list|shortlisted\s*students?|"
        r"selected\s*for\s*(next|further)|qualified)",
        re.IGNORECASE,
    )),
    ("INTERVIEW_UPDATE", re.compile(
        r"(interview\s*(scheduled|process|round|update|date)|"
        r"next\s*round|final\s*round|technical\s*interview|hr\s*round|"
        r"group\s*discussion|gd\s*(scheduled|round))",
        re.IGNORECASE,
    )),
    ("OFFER_UPDATE", re.compile(
        r"(offer\s*(letter|released|update)|final\s*selection|"
        r"selected\s*for\s*offer|congratulations)",
        re.IGNORECASE,
    )),
    ("REMINDER", re.compile(
        r"(reminder|last\s*date|deadline\s*(extended|approaching|tomorrow)|"
        r"urgent\s*(update|reminder)|final\s*call)",
        re.IGNORECASE,
    )),
    ("DRIVE_UPDATE", re.compile(
        r"(update[d\s]*:|updated\s*(information|details|schedule)|"
        r"revised|change\s*in|modification)",
        re.IGNORECASE,
    )),
    ("NEW_DRIVE", re.compile(
        r"(campus\s*(drive|hiring|recruitment|placement)|"
        r"placement\s*(drive|opportunity)|new\s*opportunity|"
        r"hiring\s*for|registration\s*open|invit(ing|ation)|"
        r"ppt\s*(announcement|scheduled|notification))",
        re.IGNORECASE,
    )),
]


def classify_email(subject: str, body: str = "") -> str:
    """Classify an email into one of the EMAIL_CLASSIFICATIONS categories."""
    combined = f"{subject} {body[:500]}"

    for classification, pattern in _CLASSIFICATION_PATTERNS:
        if pattern.search(combined):
            return classification

    return "IRRELEVANT"


# ---------------------------------------------------------------------------
# Phase 2: Follow-up Detection / Status Mapping
# ---------------------------------------------------------------------------

_STATUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OFFER_RECEIVED", re.compile(
        r"(offer\s*(letter|released)|final\s*selection\s*result|"
        r"congratulations.*selected|selected\s*candidates?\s*list)",
        re.IGNORECASE,
    )),
    ("SELECTED", re.compile(
        r"(finally?\s*selected|selection\s*list|selected\s*for\s*joining)",
        re.IGNORECASE,
    )),
    ("HR", re.compile(
        r"(hr\s*(round|interview|discussion)|"
        r"human\s*resource\s*(round|interview))",
        re.IGNORECASE,
    )),
    ("INTERVIEW", re.compile(
        r"(interview\s*(scheduled|process|round|date)|"
        r"next\s*round.*selection|technical\s*interview|"
        r"gd.*round|group\s*discussion)",
        re.IGNORECASE,
    )),
    ("SHORTLISTED", re.compile(
        r"(shortlist|short[\-\s]list|shortlisted\s*students?|"
        r"selected\s*for\s*(next|further|interview)|qualified)",
        re.IGNORECASE,
    )),
    ("OA", re.compile(
        r"(online\s*(assessment|test)|oa\s*(scheduled|date|link)|"
        r"hackerrank|coding\s*test|assessment\s*scheduled|"
        r"aptitude\s*test)",
        re.IGNORECASE,
    )),
    ("REGISTERED", re.compile(
        r"(registration\s*(successful|confirmed|complete)|"
        r"successfully\s*registered|applied\s*successfully)",
        re.IGNORECASE,
    )),
    ("REJECTED", re.compile(
        r"(not\s*shortlisted|not\s*selected|regret\s*to|"
        r"unfortunately|rejected|could\s*not\s*make)",
        re.IGNORECASE,
    )),
]


def detect_status_from_text(subject: str, body: str = "") -> str:
    """Detect placement drive status from email subject and body."""
    combined = f"{subject} {body[:500]}"

    for status, pattern in _STATUS_PATTERNS:
        if pattern.search(combined):
            return status

    return "OPEN"


# ---------------------------------------------------------------------------
# Phase 3: Rule-Based Field Extraction
# ---------------------------------------------------------------------------

# CTC patterns like "12 LPA", "12.5 Lakhs Per Annum", "Rs. 3,60,000"
_CTC_PATTERNS = [
    re.compile(r"(?:ctc|package|salary)\s*[:\-]?\s*(?:rs\.?\s*)?(\d[\d,\.]*\s*(?:lpa|lakhs?\s*(?:per\s*annum)?|crore|cr))", re.IGNORECASE),
    re.compile(r"(?:ctc|package|salary)\s*[:\-]?\s*(?:inr|rs\.?)\s*(\d[\d,\.]+(?:\s*p\.?a\.?)?)", re.IGNORECASE),
]

# Stipend patterns like "50,000 per month", "50K/month"
_STIPEND_PATTERNS = [
    re.compile(r"(?:stipend|allowance|monthly)\s*[:\-]?\s*(?:rs\.?\s*)?(\d[\d,\.]*\s*(?:per\s*month|p\.?m\.?|/\s*month|pm))", re.IGNORECASE),
    re.compile(r"(?:stipend|allowance)\s*[:\-]?\s*(?:inr|rs\.?)\s*(\d[\d,\.]+)", re.IGNORECASE),
]

# Deadline patterns
_DEADLINE_PATTERNS = [
    re.compile(r"(?:deadline|last\s*date|register\s*(?:by|before)|apply\s*(?:by|before))\s*[:\-]?\s*(\d{1,2}[\s\-/]\w+[\s\-/]\d{2,4}(?:\s+\d{1,2}[:\.]?\d{0,2}\s*(?:am|pm)?)?)", re.IGNORECASE),
    re.compile(r"(?:deadline|last\s*date)\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{4})", re.IGNORECASE),
]

# Location patterns
_LOCATION_PATTERNS = [
    re.compile(r"(?:location|work\s*location|place\s*of\s*posting|city)\s*[:\-]?\s*([A-Z][a-z]+(?:[\s,/]+[A-Z][a-z]+){0,3})", re.IGNORECASE),
]

# Registration link patterns
_LINK_PATTERNS = [
    re.compile(r"(?:registration\s*link|apply\s*(?:here|link|at)|register\s*(?:here|at))\s*[:\-]?\s*(https?://\S+)", re.IGNORECASE),
    re.compile(r"(https?://forms\.(?:gle|google\.com)/\S+)", re.IGNORECASE),
]

# Role patterns
_ROLE_PATTERNS = [
    re.compile(r"(?:role|position|designation|job\s*title)\s*[:\-]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
    re.compile(r"(?:hiring\s*for|opening\s*for|vacancy\s*for)\s+(.+?)(?:\n|,|$)", re.IGNORECASE),
]

# Category (internship/fulltime) patterns
_CATEGORY_PATTERNS = [
    (re.compile(r"\b(intern(?:ship)?|summer\s*intern(?:ship)?)\b", re.IGNORECASE), "internship"),
    (re.compile(r"\b(full[\s\-]*time|fte|ppo|pre[\s\-]*placement\s*offer)\b", re.IGNORECASE), "full_time"),
    (re.compile(r"\b(contract|freelance|part[\s\-]*time)\b", re.IGNORECASE), "contract"),
]

# Company name extraction from subjects
_COMPANY_FROM_SUBJECT = [
    re.compile(r"(?:campus\s*(?:drive|hiring|recruitment|placement)\s*[-:\|]\s*)(.+?)(?:\s*[-:\|]|\s*$)", re.IGNORECASE),
    re.compile(r"^(.+?)\s*[-:\|]\s*(?:campus|placement|hiring|recruitment|internship)", re.IGNORECASE),
    re.compile(r"(?:placement\s*(?:drive|opportunity)\s*[-:\|]\s*)(.+?)(?:\s*[-:\|]|\s*$)", re.IGNORECASE),
]


@dataclass
class RuleExtractionResult:
    """Result of rule-based extraction from an email."""
    company_name: str | None = None
    role: str | None = None
    category: str | None = None
    ctc: str | None = None
    stipend: str | None = None
    deadline: str | None = None
    location: str | None = None
    registration_link: str | None = None
    current_status: str = "OPEN"
    email_classification: str = "IRRELEVANT"
    confidence: float = 0.0
    missing_fields: list[str] = field(default_factory=list)

    @property
    def needs_gemini(self) -> bool:
        """Return True if critical fields are missing and Gemini should be called."""
        critical_missing = []
        if not self.company_name:
            critical_missing.append("company_name")
        if not self.role:
            critical_missing.append("role")
        if self.current_status == "OPEN" and self.email_classification == "IRRELEVANT":
            critical_missing.append("status")
        return len(critical_missing) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to opportunity-compatible dictionary."""
        return {
            "company_name": self.company_name,
            "role": self.role,
            "internship_or_fulltime": self.category,
            "package_or_stipend": self.ctc or self.stipend,
            "deadline": self.deadline,
            "work_location": self.location,
            "registration_link": self.registration_link,
            "current_status": self.current_status,
        }


def _first_match(patterns: list[re.Pattern[str]], text: str) -> str | None:
    """Return the first capture group from the first matching pattern."""
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def extract_from_email(
    subject: str,
    body: str = "",
    sender: str = "",
) -> RuleExtractionResult:
    """Extract placement information from email text using rules only.

    This is Phase 3: the rule-based extraction engine that runs
    BEFORE Gemini to reduce API calls by ~70%.
    """
    combined = f"{subject}\n{body}"
    result = RuleExtractionResult()

    # 1. Classify the email (Phase 13)
    result.email_classification = classify_email(subject, body)

    # 2. Detect status (Phase 2)
    result.current_status = detect_status_from_text(subject, body)

    # 3. Extract company name
    company = _first_match(_COMPANY_FROM_SUBJECT, subject)
    if company:
        result.company_name = normalize_company_name(company)

    # 4. Extract role
    result.role = _first_match(_ROLE_PATTERNS, combined)

    # 5. Extract category
    for pattern, category in _CATEGORY_PATTERNS:
        if pattern.search(combined):
            result.category = category
            break

    # 6. Extract CTC
    result.ctc = _first_match(_CTC_PATTERNS, combined)

    # 7. Extract stipend
    result.stipend = _first_match(_STIPEND_PATTERNS, combined)

    # 8. Extract deadline
    result.deadline = _first_match(_DEADLINE_PATTERNS, combined)

    # 9. Extract location
    result.location = _first_match(_LOCATION_PATTERNS, combined)

    # 10. Extract registration link
    result.registration_link = _first_match(_LINK_PATTERNS, combined)

    # Calculate confidence
    filled = sum(1 for v in [
        result.company_name, result.role, result.category,
        result.ctc or result.stipend, result.deadline, result.location,
    ] if v)
    result.confidence = filled / 6.0

    # Track missing fields
    if not result.company_name:
        result.missing_fields.append("company_name")
    if not result.role:
        result.missing_fields.append("role")
    if not result.category:
        result.missing_fields.append("category")
    if not result.ctc and not result.stipend:
        result.missing_fields.append("compensation")

    logger.info(
        "Rule extraction: company=%s role=%s status=%s confidence=%.0f%% needs_gemini=%s",
        result.company_name, result.role, result.current_status,
        result.confidence * 100, result.needs_gemini,
    )
    return result
