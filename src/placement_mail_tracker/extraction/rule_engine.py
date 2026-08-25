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

from placement_mail_tracker.utils.time import parse_datetime_flexible

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
    "eternal (zomato)": "Zomato",
    "eternal(zomato)": "Zomato",
    "zomato": "Zomato",
}


# Body-text tokens that extraction sometimes harvests in place of a real
# company name -- e.g. "@Own Location" yielding company="Location". None of
# these is ever itself a real company name, but they DO appear as
# substrings/words inside real multi-word names, so the guard below rejects
# a candidate only when *every* one of its words is one of these tokens
# (docs/design/16 Cause 6). Deliberately NOT length-based: UBS, TCS, KLA, ZF,
# Q2, WSP, SES and IPR are all real companies in production data.
#
# NOTE: docs/design/16's proposed list also includes "Unknown". Deliberately
# omitted here: "Unknown"/"Unknown Company" is an existing, deliberate
# sentinel value this codebase already stores as a literal company_name
# (scheduler.runner._UNIDENTIFIED_COMPANIES, db.manager._normalize_opportunity's
# "Unknown Company" fallback, both outside this module's edit scope) --
# rejecting it here turns that sentinel into a NOT NULL constraint violation
# instead. See this task's final report for the follow-up this implies.
_REJECTED_COMPANY_TOKENS = frozenset({
    "location", "selection", "own", "online", "test", "com",
    "details", "shortlisted",
})


def _is_rejected_company_candidate(name: str) -> bool:
    """True when every word of ``name`` is a known non-company token."""
    tokens = [t.casefold() for t in re.findall(r"[A-Za-z0-9]+", name)]
    return bool(tokens) and all(t in _REJECTED_COMPANY_TOKENS for t in tokens)


def normalize_company_name(raw: str | None) -> str | None:
    """Normalize a company name to a canonical form.

    Returns ``None`` (not ``""``) when the cleaned candidate is composed
    entirely of rejected junk tokens (docs/design/16 Cause 6) -- distinct
    from the blank-input case, which returns ``""`` as before.

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
    >>> normalize_company_name("Own Location") is None
    True
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

    if _is_rejected_company_candidate(cleaned):
        return None

    # Canonical lookup before suffix stripping
    lookup_key = cleaned.casefold()
    if lookup_key in _CANONICAL_NAMES:
        return _CANONICAL_NAMES[lookup_key]

    # Strip legal suffixes
    stripped = _STRIP_SUFFIXES.sub("", cleaned).strip()
    stripped = re.sub(r"[\s:\-–—|]+$", "", stripped)
    stripped = " ".join(stripped.split())

    # Deliberately NOT re-checking _is_rejected_company_candidate on
    # ``stripped``: every documented junk case (Location, Own Location,
    # Online Test, Selection, Shortlisted, Details, Com) is already caught
    # above, before suffix-stripping -- none of them carries a legal-entity
    # suffix word that stripping would reveal. A second check here would
    # only fire on inputs that *become* all-rejected-tokens purely as a
    # side effect of suffix removal (e.g. "Test Company" -> "Test"), which
    # risks rejecting real names for reasons no caller asked for.

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
        r"hackerrank|coding\s*test|assessment\s*(scheduled|link)|"
        # "X PPT is scheduled on ..." / "X pre placement talk is scheduled" /
        # "X - Pre-Placement Talk - 20th August" -- real, recurring CDC
        # phrasing found live (Blackrock, Accenture, American Express) that
        # the bare "ppt\s*(announcement|scheduled...)" alternative in
        # NEW_DRIVE below cannot reach: it requires PPT/the phrase to be
        # directly followed by a specific keyword, but a subject can equally
        # follow it with " - <date>" or nothing at all. The spelled-out
        # phrase "pre-placement talk" is unambiguous on its own and needs no
        # trailing qualifier; the 3-letter abbreviation "ppt" alone is kept
        # gated behind one, since it's short enough to risk an unrelated
        # false match without it. A PPT mail starting its own new Gmail
        # thread (not a reply on an already-tracked drive) has no thread_id
        # to fall back on -- classify_email() alone decides whether it
        # reaches the Phase 1 safe-attach resolver, and IRRELEVANT sits
        # outside _PLACEMENT_PROCESS_CLASSIFICATIONS, so it silently minted
        # a duplicate drive with no extracted date (docs/design/16
        # follow-up, 2026-08-18 and 2026-08-22).
        r"pre[\-\s]?placement\s*talk|"
        r"\bppt\b\s*(?:is\s*)?(?:scheduled|announcement|notification|date)|"
        # "X written test is scheduled on ..." -- real CDC phrasing (EasyReach)
        # that the bare "online\s*test" alternative above doesn't reach since
        # it isn't described as "online". Found the same way as the PPT gap:
        # classified IRRELEVANT, minted a duplicate drive (2026-08-23 audit).
        r"written\s*test\s*(?:is\s*)?scheduled)",
        re.IGNORECASE,
    )),
    ("SHORTLIST_UPDATE", re.compile(
        r"(shortlist|short\s*list|shortlisted\s*students?|"
        r"selected\s*for\s*(next|further)|qualified|"
        # "Kind Attn: X Applied Students - Assignment Submission" -- an
        # assignment mail implies the student is already past the shortlist
        # stage. Found live: this exact phrasing classified IRRELEVANT and
        # created a junk-named drive ("Kind Attn") instead of attaching to
        # the real one (docs/design/16 follow-up, 2026-08-22).
        r"assignment\s*(?:submission|deadline|round))",
        re.IGNORECASE,
    )),
    ("INTERVIEW_UPDATE", re.compile(
        r"(interview\s*(scheduled|process|round|update|date)|"
        r"next\s*round|final\s*round|technical\s*interview|hr\s*round|"
        r"group\s*discussion|gd\s*(scheduled|round)|"
        # "X selection process is scheduled on <date>" -- real, recurring CDC
        # phrasing for an onsite test+interview day (Valuelabs, Valeo,
        # WorkIndia, BluBridge, Novac, BorgWarner) with none of "interview"/
        # "round"/"GD" in the subject. Anchored on "scheduled" directly next
        # to "selection process" so a NEW_DRIVE announcement merely
        # *describing* its selection process (no date yet) doesn't misroute
        # here (2026-08-23 audit).
        r"selection\s*process\s*(?:is\s*)?scheduled)",
        re.IGNORECASE,
    )),
    ("OFFER_UPDATE", re.compile(
        r"(offer\s*(letter|released|update)|final\s*selection|"
        r"selected\s*for\s*offer|congratulations|"
        # "X Dream/PPO/Regular/Core Internship selection list" -- real,
        # recurring CDC naming (Bosch, Flipkart, Fidas) for a results list
        # with no "offer"/"final selection"/"congratulations" wording.
        # Scoped to the tier vocabulary actually observed rather than a bare
        # "selection list", to avoid sweeping in an unrelated (e.g.
        # eligibility) "selection list" (2026-08-23 audit).
        r"(?:dream|ppo|regular|core)\w*.{0,40}?selection\s*list)",
        re.IGNORECASE,
    )),
    ("REMINDER", re.compile(
        r"(reminder|last\s*date|deadline\s*(extended|approaching|tomorrow)|"
        r"urgent\s*(update|reminder)|final\s*call|"
        # Bare "Deadline : <weekday>, <date>" with none of the qualifier
        # words above -- real, live CDC phrasing (American Express:
        # "Applications Lines - Deadline : Friday, 21st Aug, 2:00pm").
        # Deliberately anchored on the weekday name, not just "deadline:" --
        # a NEW_DRIVE posting's own structured body routinely has its own
        # "Deadline: 5 June 2027" field with no weekday, and that must keep
        # classifying NEW_DRIVE, not get reclassified as a reminder. Found
        # the same day as the PPT gap above and by the same mechanism: it
        # classified IRRELEVANT, which sits outside
        # _PLACEMENT_PROCESS_CLASSIFICATIONS, so it never reached the
        # Phase 1 safe-attach resolver and minted a duplicate drive instead
        # of attaching to the real one.
        r"deadline\s*:\s*"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))",
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


# ---------------------------------------------------------------------------
# Drive-kind classification (docs/design/16, Phase 2)
# ---------------------------------------------------------------------------

# Order matters: first match wins. Keyword rule, not Gemini — deterministic
# and free, per CLAUDE.md operating principle 3.
_DRIVE_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("HACKATHON", re.compile(
        r"\b(hackathon|ideathon|innohack|innovation\s+challenge|"
        r"innovation\s+hack)\b",
        re.IGNORECASE,
    )),
    ("RESEARCH", re.compile(
        r"\b(ph\.?\s*d\b|post[\-\s]?doc(?:toral)?|research\s+opportunit)",
        re.IGNORECASE,
    )),
    ("SCHOLARSHIP", re.compile(r"\bscholarship", re.IGNORECASE)),
    ("WORKSHOP", re.compile(r"\b(workshop|bootcamp)\b", re.IGNORECASE)),
    # Learning-series / info-session mail that is not a hiring drive at all
    # (docs/design/16 Cause 4) -- e.g. "Deloitte US-India's BRIDGE Campus
    # Learning Series | Registrations now open" was slipping through as
    # PLACEMENT and minting a phantom OA. "bootcamp" deliberately stays in
    # WORKSHOP above (already there, matched first) rather than duplicated
    # here, to avoid silently reclassifying existing WORKSHOP history.
    # "campus connect" is deliberately NOT in this list -- see
    # _CAMPUS_CONNECT_RE below (safety-nets plan, Phase 5).
    ("WEBINAR", re.compile(
        r"\b(learning\s+series|webinar|"
        r"info(?:rmation)?\s*session|tech\s*talk|masterclass|"
        r"awareness\s+session)\b",
        re.IGNORECASE,
    )),
]

# "campus connect" is company-branding language, not a mail-type phrase like
# the others above -- Honeywell names both its hackathon AND its genuine
# placement drives this way. Unlike the unambiguous phrases in
# _DRIVE_KIND_PATTERNS, it is only a WEBINAR signal in the ABSENCE of
# ordinary hiring-drive vocabulary; a real drive that happens to brand
# itself "Campus Connect" (package, CGPA cutoff, registration deadline, ...)
# must still classify as PLACEMENT and reach the calendar (safety-nets plan,
# Phase 5 -- this is the "watch" item that motivated the change: currently
# harmless only because the one production mail using this phrase is
# Honeywell's hackathon, which HACKATHON matches first in the loop above).
_CAMPUS_CONNECT_RE = re.compile(r"\bcampus\s+connect\b", re.IGNORECASE)
_DRIVE_VOCAB_RE = re.compile(
    r"(registration\s+deadline|\bctc\b|\bstipend\b|\bpackage\b|"
    r"\blpa\b|\blakhs?\b|\bcgpa\b|eligibility\s+criteria|selection\s+process)",
    re.IGNORECASE,
)

# A mail carrying one of these classifications is, by construction, mid-way
# through an actual placement process (an OA got scheduled, a shortlist was
# published, ...) for whichever drive it's attached to. That is stronger
# evidence than a keyword collision, and must win: a stray/misattributed
# "...Hackathon..." mail landing on a real drive's row (a separate
# company-fuzzy-match issue, docs/design/15 §1) must not silently hide that
# drive's OA/interview/offer events from the calendar.
_PLACEMENT_PROCESS_CLASSIFICATIONS = frozenset({
    "OA_UPDATE", "SHORTLIST_UPDATE", "INTERVIEW_UPDATE", "OFFER_UPDATE",
    "APPLICATION_CONFIRMATION", "DRIVE_UPDATE", "REMINDER",
})


def classify_drive_kind(subject: str, body: str = "", email_classification: str = "") -> str:
    """Classify a mail as a placement drive vs. a non-drive opportunity.

    ponytail: a fixed keyword list, not a learned classifier — good enough
    to catch the hackathon/scholarship/workshop/research mail observed in
    live data (docs/design/16). Widen the pattern list if a new non-drive
    category shows up in NOT_ELIGIBLE-free, ELIGIBLE-marked noise.
    """
    if email_classification in _PLACEMENT_PROCESS_CLASSIFICATIONS:
        return "PLACEMENT"
    combined = f"{subject} {body[:300]}"
    for kind, pattern in _DRIVE_KIND_PATTERNS:
        if pattern.search(combined):
            return kind
    if _CAMPUS_CONNECT_RE.search(combined) and not _DRIVE_VOCAB_RE.search(combined):
        return "WEBINAR"
    return "PLACEMENT"


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

# OA-date pattern for the standard VIT CDC phrasing "DD-MM-YYYY By H.MMPm"
# (e.g. "18-08-2026 By 2.30Pm", "on 05-08-2026 by 1.30pm") -- previously
# unparsed (docs/design/16 Cause 6). Built deterministically from the regex
# groups rather than via the fuzzy dateutil parser: "2.30Pm" is exactly the
# kind of ambiguous fragment that parser accepts unreliably.
_OA_DATE_RE = re.compile(
    r"\b(\d{1,2})-(\d{1,2})-(\d{4})\s+by\s+(\d{1,2})[.:](\d{2})\s*([AaPp][Mm])\b",
    re.IGNORECASE,
)


def _extract_oa_date(text: str) -> str | None:
    """Return ``YYYY-MM-DDTHH:MM`` for the CDC "DD-MM-YYYY By H.MMPm" phrasing."""
    match = _OA_DATE_RE.search(text)
    if not match:
        return None
    day, month, year, hour, minute, meridiem = match.groups()
    day, month, year, hour, minute = int(day), int(month), int(year), int(hour), int(minute)
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1 <= hour <= 12 and 0 <= minute < 60):
        return None
    if meridiem.lower() == "pm" and hour != 12:
        hour += 12
    elif meridiem.lower() == "am" and hour == 12:
        hour = 0
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}"


# "X pre placement talk is scheduled on 24th August 2026 3.30 pm @ Anna
# Auditorium" / "...24.08.2026 by 11:30 am at the respective venues" /
# "...27th August 2026 by 10.30 am - ANNA Auditorium" -- real, recurring CDC
# phrasing (Unilever, Accenture, Cognizant, 2026-08-23/25). The venue text
# that follows the date is unbounded and keeps taking new shapes ("@ X",
# "at the respective venues", "- X Auditorium", ...) -- an earlier version
# of this regex tried to enumerate stop-phrases for each one and missed
# Cognizant's "- ANNA Auditorium" dash form entirely (dateutil's fuzzy
# parser silently returns None on trailing junk it can't place, rather than
# raising, so a missed stop-phrase fails this quietly). Capturing only a
# strict date/time shape directly -- never "everything up to a delimiter"
# -- sidesteps needing to know what any given CDC mail's venue phrasing
# looks like at all. Normalized (ordinal suffixes stripped, "H.MM am/pm" ->
# "H:MM am/pm") and handed to parse_datetime_flexible for the actual date
# math. A PPT date is deliberately its own field, not folded into
# oa_date/interview_date -- neither is what actually happens at a PPT, and
# conflating them previously meant a PPT mail's date went nowhere.
_MONTH_NAMES_RE = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)"
)
_PPT_DATE_FRAGMENT_RE = re.compile(
    r"pre[\-\s]?placement\s*talk\s+is\s+scheduled\s+on\s+"
    r"(\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH_NAMES_RE + r"\s+\d{4}"
    r"(?:\s*(?:by\s+)?\d{1,2}[.:]\d{2}\s*[ap]m)?"
    r"|\d{1,2}\.\d{1,2}\.\d{4}\s+by\s+\d{1,2}[.:]\d{2}\s*[ap]m)",
    re.IGNORECASE,
)
_ORDINAL_SUFFIX_RE = re.compile(r"(\d)(st|nd|rd|th)\b", re.IGNORECASE)
_DOTTED_TIME_RE = re.compile(r"\b(\d{1,2})\.(\d{2})\s*([ap]m)\b", re.IGNORECASE)


def _extract_ppt_date(text: str) -> str | None:
    """Return an ISO-ish datetime string for a pre-placement-talk mail."""
    match = _PPT_DATE_FRAGMENT_RE.search(text)
    if not match:
        return None
    fragment = _ORDINAL_SUFFIX_RE.sub(r"\1", match.group(1))
    fragment = _DOTTED_TIME_RE.sub(r"\1:\2 \3", fragment)
    parsed = parse_datetime_flexible(fragment)
    if parsed is None:
        return None
    if parsed.hour == 0 and parsed.minute == 0 and ":" not in fragment:
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%dT%H:%M")


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
    # VIT CDC shortlist-notice subjects: "Hindustan Unilever - Bio Tech
    # shortlisted list - Reg", "Clayfin - Shortlisted list - Reg". Appended
    # last so it only fires when nothing more specific already matched.
    re.compile(
        r"^(.+?)\s*[–—\-]\s*.*?\b(?:shortlist(?:ed)?|selection|selected)\s*list\b",
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
    oa_date: str | None = None
    ppt_date: str | None = None
    location: str | None = None
    registration_link: str | None = None
    current_status: str = "OPEN"
    email_classification: str = "IRRELEVANT"
    confidence: float = 0.0
    missing_fields: list[str] = field(default_factory=list)
    degree_level: str = "UNKNOWN"
    branches_allowed: list[str] = field(default_factory=list)
    drive_kind: str = "PLACEMENT"

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
            "oa_date": self.oa_date,
            "ppt_date": self.ppt_date,
            "work_location": self.location,
            "registration_link": self.registration_link,
            "current_status": self.current_status,
            "degree_level": self.degree_level,
            "branches_allowed": self.branches_allowed if self.branches_allowed else None,
            "drive_kind": self.drive_kind,
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

    # 2b. Classify drive kind (placement vs. hackathon/scholarship/etc.)
    result.drive_kind = classify_drive_kind(subject, body, result.email_classification)

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

    # 8b. Extract OA date ("DD-MM-YYYY By H.MMPm" -- the standard VIT CDC
    # phrasing; docs/design/16 Cause 6). Independent of email_classification:
    # this is a narrow, deterministic date format, not a generic date guess.
    result.oa_date = _extract_oa_date(combined)
    result.ppt_date = _extract_ppt_date(combined)

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
