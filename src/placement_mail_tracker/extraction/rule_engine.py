"""Rule-based extraction engine for placement emails.

Extracts company names, CTC, stipend, deadlines, locations, roles,
and status updates from email text using regex and keyword matching.
Only falls back to Gemini when critical fields are missing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

logger = logging.getLogger(__name__)

# D1 (docs/design/10-confirmation-and-reminders.md): the sender gate for
# APPLICATION_CONFIRMATION. Defined here (a leaf module with no internal
# package dependencies) so both gmail/filters.py and extraction/confirmation.py
# can import it without creating an import cycle.
CONFIRMATION_SENDER = "noreply.cdcinfo@vitstudent.ac.in"


def is_confirmation_sender(sender: str) -> bool:
    """Exact match on the CDC confirmation address, case-insensitive."""
    _, address = parseaddr(sender or "")
    return address.strip().lower() == CONFIRMATION_SENDER

# ---------------------------------------------------------------------------
# Company Normalization
# ---------------------------------------------------------------------------

# Common legal-entity suffixes and filler words to strip
_STRIP_SUFFIXES = re.compile(
    r"\b(inc|ltd|llc|llp|pvt|private|limited|"
    r"technologies|technology|tech|solutions|services|systems|"
    r"global|india|corp|corporation|group|holdings|enterprises|"
    r"co|company|consultants|consulting)\b\.?",
    re.IGNORECASE,
)

# Placement-drive tier / label noise that contaminates extracted company names.
# E.g. "Clayfin Regular", "Cisco: FY27 Pre-Placement Talk", "Dream Internship Drive".
_COMPANY_NOISE = re.compile(
    r"\b("
    r"super\s+dream|dream\s+intern(?:ship)?"
    r"|regular|normal"
    r"|fy\s*\d{2,4}"
    r"|pre[\s\-]*placement(?:\s+talk)?|pre(?=\s*$|\s*:)"  # "Pre" as standalone suffix/label
    r"|placement\s+talk|ppt|talk"
    r")\b",
    re.IGNORECASE,
)

# Label-colon prefixes to strip from the front of extracted company names.
# Handles "Drive: Microsoft", "Opportunity: Google", as well as the common
# email subject prefixes already present ("Re:", "Updated:", …).
_LABEL_PREFIX = re.compile(
    r"^(updated?|reminder|re|fwd?|drive|campus|opportunity|recruitment|placement|"
    r"join(?:\s+immediately)?)"
    r"\s*[:\-]\s*",
    re.IGNORECASE,
)


def _smart_title(text: str) -> str:
    """Title-case while preserving all-uppercase acronyms (UBS, WSP, JW)."""
    tokens = text.split()
    if not tokens:
        return text
    all_upper = all(t.isupper() for t in tokens)
    out = []
    for tok in tokens:
        if not all_upper and tok.isupper() and 2 <= len(tok) <= 4:
            out.append(tok)
        elif len(tokens) == 1 and tok.isupper() and 2 <= len(tok) <= 5:
            out.append(tok)
        else:
            out.append(tok.capitalize())
    return " ".join(out)

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
    "jw": "JW Consultants",
    "jw consultants": "JW Consultants",
    "cisco": "Cisco",
    "clayfin": "Clayfin",
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
    >>> normalize_company_name("Cisco: FY27 Pre-Placement Talk")
    'Cisco'
    >>> normalize_company_name("Clayfin Regular")
    'Clayfin'
    """
    if not raw:
        return ""

    cleaned = raw.strip()

    # Strip label-colon prefixes: "Drive: …", "Re: …", "Updated: …", etc.
    cleaned = _LABEL_PREFIX.sub("", cleaned)

    # Strip placement-tier noise: "FY27", "Pre-Placement Talk", "Regular", etc.
    cleaned = _COMPANY_NOISE.sub(" ", cleaned)

    # Strip stray leading/trailing punctuation left by noise removal.
    cleaned = re.sub(r"^[\s:\-–—|]+|[\s:\-–—|]+$", "", cleaned)

    # Collapse internal spaces
    cleaned = " ".join(cleaned.split())

    if not cleaned:
        return ""

    # Canonical lookup before suffix stripping
    lookup_key = cleaned.casefold()
    if lookup_key in _CANONICAL_NAMES:
        return _CANONICAL_NAMES[lookup_key]

    # Strip legal suffixes
    stripped = _STRIP_SUFFIXES.sub("", cleaned).strip()
    stripped = re.sub(r"[\s:\-–—|]+$", "", stripped)
    stripped = " ".join(stripped.split())

    # Canonical lookup after suffix stripping
    if stripped:
        lookup_key = stripped.casefold()
        if lookup_key in _CANONICAL_NAMES:
            return _CANONICAL_NAMES[lookup_key]
        return _smart_title(stripped)
    return _smart_title(cleaned)


# ---------------------------------------------------------------------------
# Email Classification
# ---------------------------------------------------------------------------

EMAIL_CLASSIFICATIONS = (
    "NEW_DRIVE",
    "DRIVE_UPDATE",
    "OA_UPDATE",
    "SHORTLIST_UPDATE",
    "INTERVIEW_UPDATE",
    "APPLICATION_CONFIRMATION",
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


def classify_email(subject: str, body: str = "", sender: str = "") -> str:
    """Classify an email into one of the EMAIL_CLASSIFICATIONS categories.

    D1: the sender gate is checked before the ordered pattern list (which
    contains OFFER_UPDATE's bare "congratulations" pattern), so a confirmation
    mail phrased "Congratulations, your application has been submitted" can
    never misfire as OFFER_UPDATE — it never reaches that pattern at all.
    """
    if sender and is_confirmation_sender(sender):
        return "APPLICATION_CONFIRMATION"

    combined = f"{subject} {body[:500]}"

    for classification, pattern in _CLASSIFICATION_PATTERNS:
        if pattern.search(combined):
            return classification

    return "IRRELEVANT"


# ---------------------------------------------------------------------------
# Follow-up Detection / Status Mapping
# ---------------------------------------------------------------------------

_STATUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("WITHDRAWN", re.compile(
        r"(cancel(?:led|lation)|withdrawn|withdraw|hiring\s*freeze|"
        r"drive\s*(?:closed|cancelled|withdrawn)|process\s*suspended|"
        r"registration\s*cancel(?:led)?|not\s*proceeding)",
        re.IGNORECASE,
    )),
    ("REJECTED", re.compile(
        r"(not\s*shortlisted|not\s*selected|regret\s*to|"
        r"unfortunately|rejected|could\s*not\s*make)",
        re.IGNORECASE,
    )),
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
    ("OA", re.compile(
        r"(online\s*(assessment|test)|oa\s*(scheduled|date|link)|"
        r"hackerrank|coding\s*test|assessment\s*scheduled|"
        r"aptitude\s*test)",
        re.IGNORECASE,
    )),
    ("SHORTLISTED", re.compile(
        r"(shortlist|short[\-\s]list|shortlisted\s*students?|"
        r"selected\s*for\s*(next|further|interview)|qualified)",
        re.IGNORECASE,
    )),
    ("INTERVIEW", re.compile(
        r"(interview\s*(scheduled|process|round|date)|"
        r"next\s*round.*selection|technical\s*interview|"
        r"gd.*round|group\s*discussion)",
        re.IGNORECASE,
    )),
    ("REGISTERED", re.compile(
        r"(registration\s*(successful|confirmed|complete)|"
        r"successfully\s*registered|applied\s*successfully)",
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
# Rule-Based Field Extraction
# ---------------------------------------------------------------------------

# CTC patterns like "12 LPA", "12.5 Lakhs Per Annum", "Rs. 3,60,000"
_CTC_PATTERNS = [
    re.compile(
        r"(?:ctc|package|salary)\s*[:\-]?\s*(?:rs\.?\s*)?"
        r"(\d[\d,\.]*\s*(?:lpa|lakhs?\s*(?:per\s*annum)?|crore|cr))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:ctc|package|salary)\s*[:\-]?\s*(?:inr|rs\.?)\s*"
        r"(\d[\d,\.]+(?:\s*p\.?a\.?)?)",
        re.IGNORECASE,
    ),
]

# Stipend patterns like "50,000 per month", "50K/month"
_STIPEND_PATTERNS = [
    re.compile(
        r"(?:stipend|allowance|monthly)\s*[:\-]?\s*(?:rs\.?\s*)?"
        r"(\d[\d,\.]*\s*(?:per\s*month|p\.?m\.?|/\s*month|pm))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:stipend|allowance)\s*[:\-]?\s*(?:inr|rs\.?)\s*(\d[\d,\.]+)",
        re.IGNORECASE,
    ),
]

# Deadline patterns
_DEADLINE_PATTERNS = [
    re.compile(
        r"(?:deadline|last\s*date|register\s*(?:by|before)|apply\s*(?:by|before))"
        r"\s*[:\-]?\s*"
        r"(\d{1,2}[\s\-/]\w+[\s\-/]\d{2,4}"
        r"(?:\s+\d{1,2}[:\.]?\d{0,2}\s*(?:am|pm)?)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:deadline|last\s*date)\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{4})",
        re.IGNORECASE,
    ),
]

# Location patterns
_LOCATION_PATTERNS = [
    re.compile(
        r"(?:location|work\s*location|place\s*of\s*posting|city)\s*[:\-]?\s*"
        r"([A-Z][a-z]+(?:[\s,/]+[A-Z][a-z]+){0,3})",
        re.IGNORECASE,
    ),
]

# Registration link patterns
_LINK_PATTERNS = [
    re.compile(
        r"(?:registration\s*link|apply\s*(?:here|link|at)|register\s*(?:here|at))"
        r"\s*[:\-]?\s*(https?://\S+)",
        re.IGNORECASE,
    ),
    re.compile(r"(https?://forms\.(?:gle|google\.com)/\S+)", re.IGNORECASE),
]

# Role patterns — capped at 60 chars to avoid grabbing full table-header lines.
_ROLE_PATTERNS = [
    re.compile(
        r"(?:role|position|designation|job\s*title)\s*[:\-]?\s*"
        r"([A-Za-z][A-Za-z0-9\s&/\-\.]{1,58}?)(?=\s*[\|;,\n]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:hiring\s*for|opening\s*for|vacancy\s*for)\s+"
        r"([A-Za-z][A-Za-z0-9\s&/\-\.]{1,48}?)(?=\s*[,;|\n]|$)",
        re.IGNORECASE,
    ),
    # VIT CDC selection-list subjects: "Philips Super Dream Internship selection list 2027 Batch"
    re.compile(
        r"((?:super\s+dream|dream(?:\s+offer)?|core|regular)\s*intern(?:ship)?)"
        r"\s+selection\s+list",
        re.IGNORECASE,
    ),
    # VIT CDC batch-suffix subjects: "PowerSchool Dream offer Internship - 2027 Batch"
    re.compile(
        r"((?:super\s+dream|dream(?:\s+offer)?|core|regular)\s*intern(?:ship)?)"
        r"\s*[-–]\s*\d{4}\s*batch",
        re.IGNORECASE,
    ),
]

# Words that indicate the extracted "role" is actually a table header or tier
# label rather than a real job title.
_ROLE_NOISE = re.compile(
    r"\b(name|qualification|background|passing\s*year|eligibility|"
    r"category|gender|dob|date\s+of\s+birth|graduation\s+year|"
    r"b\.?\s*tech|m\.?\s*tech|cgpa|cpi|percentage)\b",
    re.IGNORECASE,
)


def _clean_role(raw: str | None) -> str | None:
    """Return None when the extracted role looks like a table header or tier label."""
    if not raw:
        return None
    stripped = raw.strip()
    # Too long to be a real role title
    if len(stripped) > 80:
        return None
    # Contains table-header / tier-label words
    if _ROLE_NOISE.search(stripped):
        return None
    return stripped or None

# Category (internship/fulltime) patterns
_CATEGORY_PATTERNS = [
    (
        re.compile(r"\b(intern(?:ship)?|summer\s*intern(?:ship)?)\b", re.IGNORECASE),
        "internship",
    ),
    (
        re.compile(
            r"\b(full[\s\-]*time|fte|ppo|pre[\s\-]*placement\s*offer)\b",
            re.IGNORECASE,
        ),
        "full_time",
    ),
    (re.compile(r"\b(contract|freelance|part[\s\-]*time)\b", re.IGNORECASE), "contract"),
]

# Company name extraction from subjects
_COMPANY_FROM_SUBJECT = [
    # "Congratulations!! Philips Super Dream Internship selection list 2027 Batch"
    re.compile(
        r"^congratulations[!!*\s]*"
        r"([A-Za-z][A-Za-z\s]+?)\s+"
        r"(?:super\s+dream|dream(?:\s+offer)?|core|regular)\s*intern(?:ship)?",
        re.IGNORECASE,
    ),
    # "PowerSchool Dream offer Internship - 2027 Batch"
    # (excludes subjects starting with category words)
    re.compile(
        r"^(?!(?:super\s+dream|dream|core|regular)\b)"
        r"([A-Za-z][A-Za-z\s]+?)\s+"
        r"(?:super\s+dream|dream(?:\s+offer)?|core|regular)\s*intern(?:ship)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:campus\s*(?:drive|hiring|recruitment|placement)\s*[–—\-:\|]\s*)"
        r"(.+?)(?:\s*[–—\-:\|]|\s*$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(.+?)\s*[–—\-:\|]\s*"
        r"(?:campus|placement|hiring|recruitment|internship)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:placement\s*(?:drive|opportunity)\s*[–—\-:\|]\s*)"
        r"(.+?)(?:\s*[–—\-:\|]|\s*$)",
        re.IGNORECASE,
    ),
]

_DEGREE_LEVEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "MTECH": re.compile(
        r"\b(m\.?\s*tech(?:nology)?|post[\s\-]*grad(?:uate)?|"
        r"masters?\s+(?:degree|students?|program))\b",
        re.IGNORECASE,
    ),
    "BTECH": re.compile(
        r"\b(b\.?\s*tech(?:nology)?|under[\s\-]*grad(?:uate)?|bachelor(?:s)?)\b",
        re.IGNORECASE,
    ),
}

_COMPANY_FROM_BODY_PATTERNS = [
    re.compile(
        r"(?:company|organization|employer)\s*[:\-]?\s*"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:welcome to|hiring for)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
        re.IGNORECASE,
    ),
]

# ---------------------------------------------------------------------------
# Branch Extraction
# ---------------------------------------------------------------------------

# Canonical branch names mapped from their common aliases (lowercase keys).
_BRANCH_ALIASES: dict[str, str] = {
    # CSE
    "computer science and engineering": "CSE",
    "computer science & engineering": "CSE",
    "computer science engineering": "CSE",
    "computer science": "CSE",
    "cse": "CSE",
    "cs": "CSE",
    # IT
    "information technology": "IT",
    "it": "IT",
    # AI & ML
    "artificial intelligence and machine learning": "AI&ML",
    "artificial intelligence & machine learning": "AI&ML",
    "artificial intelligence": "AI&ML",
    "ai and ml": "AI&ML",
    "ai & ml": "AI&ML",
    "ai&ml": "AI&ML",
    "ai/ml": "AI&ML",
    "aiml": "AI&ML",
    "ai": "AI&ML",  # standalone "AI" branch (VIT CDC pattern)
    # Data Science
    "data science": "Data Science",
    "cse ds": "Data Science",
    "computer science with data science": "Data Science",
    "cse - data science": "Data Science",
    "cse(data science)": "Data Science",
    # Cyber Security
    "cyber security": "Cyber Security",
    "cybersecurity": "Cyber Security",
    "cse - cyber security": "Cyber Security",
    "information security": "Cyber Security",
    # ECE
    "electronics and communication engineering": "ECE",
    "electronics and communication": "ECE",
    "electronics & communication engineering": "ECE",
    "electronics & communication": "ECE",
    "ece": "ECE",
    # EEE
    "electrical and electronics engineering": "EEE",
    "electrical and electronics": "EEE",
    "eee": "EEE",
    # Mechanical
    "mechanical engineering": "Mechanical",
    "mechanical": "Mechanical",
    "mech": "Mechanical",
    # Civil
    "civil engineering": "Civil",
    "civil": "Civil",
    # Chemical
    "chemical engineering": "Chemical",
    "chemical": "Chemical",
    # Production
    "production and industrial engineering": "Production",
    "production engineering": "Production",
    "production": "Production",
}

_ALL_BRANCHES_RE = re.compile(
    r"\ball\s+(?:b\.?\s*tech|branches?|departments?|streams?|engineering|eligible|programs?|students?)\b"
    r"|\bopen\s+to\s+all\b"
    r"|\bany\s+(?:branch|department|engineering)\b",
    re.IGNORECASE,
)

# Matches the header of a branch-eligibility section and captures the branch list text.
_BRANCH_SECTION_RE = re.compile(
    r"(?:eligible\s+branches?|open\s+to\s*:|applicable\s+(?:for|to)\s*:|"
    r"branches?\s*(?:eligible|allowed|considered)?\s*:|"
    r"departments?\s*:|academic\s+programs?\s*:|"
    r"(?:b\.?\s*tech|m\.?\s*tech)\s+branches?\s*:|"
    r"students?\s+from\s*:|candidates?\s+from\s*:|open\s+for\s*:|"
    r"(?:the\s+)?following\s+branches?\s*(?:are\s+eligible)?\s*:)"
    r"[ \t]*[:–\-]?[ \t]*(.{3,250}?)(?:\n|$)",
    re.IGNORECASE,
)


_TRAILING_NOISE_RE = re.compile(
    r"\s+\b(?:only|students?|branches?|related|and|or|etc\.?|departments?|year|yrs?|[&()])\b\s*$",
    re.IGNORECASE,
)


_DEGREE_PREFIX_RE = re.compile(
    r"^(?:m\.?\s*tech|m\.?\s*sc|mca|b\.?\s*tech|b\.?\s*e)\s+(?:\d+\s*(?:year|yr|yrs?)\s+)?",
    re.IGNORECASE,
)


def _normalize_branch(raw: str) -> str | None:
    """Return canonical branch name or None if unrecognized."""
    text = re.sub(r"[()[\]]", " ", raw.strip()).lower()
    text = re.sub(r"\s+", " ", text).strip()
    # Strip trailing noise words repeatedly (handles "related branches only")
    while True:
        cleaned = _TRAILING_NOISE_RE.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    if not text or len(text) < 2:
        return None
    if text in _BRANCH_ALIASES:
        return _BRANCH_ALIASES[text]

    # Strip degree-program prefix ("M.Tech 2 year CSE" → "CSE")
    text_no_prefix = _DEGREE_PREFIX_RE.sub("", text, count=1).strip()
    if text_no_prefix and text_no_prefix != text:
        if text_no_prefix in _BRANCH_ALIASES:
            return _BRANCH_ALIASES[text_no_prefix]

    # Longest-alias word-boundary match (≥3 chars to avoid false positives)
    best: str | None = None
    best_len = 0
    for alias, canon in _BRANCH_ALIASES.items():
        if len(alias) < 3:
            continue
        if re.search(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE) and (
            len(alias) > best_len
        ):
            best = canon
            best_len = len(alias)
    return best


def _extract_branches_from_section(section_text: str) -> list[str]:
    """Parse a branch-section fragment into a deduplicated list of canonical names."""
    if _ALL_BRANCHES_RE.search(section_text):
        return ["All Branches"]
    # Split on delimiters AND on " and " (handles "CSE and IT students" style lists)
    raw_parts = re.split(r"[,/|;]+|\s+and\s+", section_text, flags=re.IGNORECASE)
    seen: set[str] = set()
    result: list[str] = []
    for part in raw_parts:
        part = part.strip()
        canon = _normalize_branch(part)
        if canon and canon not in seen:
            result.append(canon)
            seen.add(canon)
    return result


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
    degree_level: str = "UNKNOWN"
    branches_allowed: list[str] = field(default_factory=list)

    @property
    def needs_gemini(self) -> bool:
        """Return True if critical fields are missing and Gemini should be called."""
        critical_missing = []
        is_follow_up = self.email_classification in (
            "OA_UPDATE", "INTERVIEW_UPDATE", "SHORTLIST_UPDATE", "OFFER_UPDATE", "DRIVE_UPDATE"
        )
        if not self.company_name:
            critical_missing.append("company_name")
        if not self.role:
            if not (is_follow_up and self.company_name and self.current_status != "OPEN"):
                critical_missing.append("role")
        if self.current_status == "OPEN" and self.email_classification == "IRRELEVANT":
            critical_missing.append("status")
        # oa_date/interview_date have no rule-based extraction path at all (no
        # field even exists on this dataclass) — Gemini is the only way to get
        # them, so a mail whose entire purpose is announcing one must always
        # be sent to Gemini, regardless of how confident the rest of the
        # extraction looks.
        if self.email_classification in ("OA_UPDATE", "INTERVIEW_UPDATE"):
            critical_missing.append(self.email_classification)
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
            "degree_level": self.degree_level,
            "branches_allowed": self.branches_allowed if self.branches_allowed else None,
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

    Runs before Gemini to reduce API calls by ~70%.
    """
    combined = f"{subject}\n{body}"
    result = RuleExtractionResult()

    # 1. Classify the email
    result.email_classification = classify_email(subject, body, sender)

    # 2. Detect status
    result.current_status = detect_status_from_text(subject, body)

    # 3. Extract company name
    ext_source = "SUBJECT"
    company = _first_match(_COMPANY_FROM_SUBJECT, subject)
    if not company:
        ext_source = "BODY"
        company = _first_match(_COMPANY_FROM_BODY_PATTERNS, body[:200])

    if company:
        result.company_name = normalize_company_name(company)
        logger.info("Extracted company '%s' from %s", result.company_name, ext_source)

    # 4. Extract role (filtered to reject table headers / tier labels)
    result.role = _clean_role(_first_match(_ROLE_PATTERNS, combined))

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

    # 11. Detect degree level
    has_mtech = bool(_DEGREE_LEVEL_PATTERNS["MTECH"].search(combined))
    has_btech = bool(_DEGREE_LEVEL_PATTERNS["BTECH"].search(combined))
    if has_mtech and has_btech:
        result.degree_level = "ANY"
    elif has_mtech:
        result.degree_level = "MTECH"
    elif has_btech:
        result.degree_level = "BTECH"

    # 12. Extract eligible branches from structured section headers
    branch_match = _BRANCH_SECTION_RE.search(combined)
    if branch_match:
        extracted = _extract_branches_from_section(branch_match.group(1))
        if extracted:
            result.branches_allowed = extracted
            # Infer degree from branch-section context when still unknown
            if result.degree_level == "UNKNOWN":
                ctx_start = max(0, branch_match.start() - 40)
                ctx = combined[ctx_start : branch_match.end()]
                if _DEGREE_LEVEL_PATTERNS["MTECH"].search(ctx):
                    result.degree_level = "MTECH"
                else:
                    result.degree_level = "BTECH"
    elif _ALL_BRANCHES_RE.search(combined[:400]):
        # "Open to all B.Tech students" near the top of the email — restrict to first 400 chars
        # to avoid false positives from phrases like "all students are invited" in general prose
        result.branches_allowed = ["All Branches"]
        if result.degree_level == "UNKNOWN":
            if _DEGREE_LEVEL_PATTERNS["BTECH"].search(combined):
                result.degree_level = "BTECH"
            elif not _DEGREE_LEVEL_PATTERNS["MTECH"].search(combined):
                result.degree_level = "BTECH"  # default for "all branches" without degree spec

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
