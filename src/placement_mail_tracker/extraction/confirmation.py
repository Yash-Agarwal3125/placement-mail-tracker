"""Detection and matching for CDC application-confirmation mails.

Feature 1 (docs/design/10-confirmation-and-reminders.md). Zero real samples
exist yet (docs/design/08-confirmation-audit.md blocker 1) — detection is
built defensively broad (the sender gate is what makes broad body-matching
safe: docs/design/08 A2/D1), matching is built conservatively narrow (a much
higher bar than the fuzzy-dedup path used for sheet display, since this one
drives an automatic status write instead of a human-reviewed row).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from placement_mail_tracker.extraction.rule_engine import (
    CONFIRMATION_SENDER,
    is_confirmation_sender,
    normalize_company_name,
)

__all__ = [
    "CONFIRMATION_SENDER",
    "is_confirmation_sender",
    "CONFIRMED_PATTERN_FAMILIES",
    "detect_confirmation_tier",
    "extract_reference_id",
    "ConfirmationMatch",
    "find_confident_drive_match",
    "FUZZY_MATCH_THRESHOLD",
    "FUZZY_UNIQUENESS_MARGIN",
]


# Named pattern families for the CONFIRMED tier (feature_1_spec). Named so
# logs show which family matched a real mail once one arrives — needed to
# refine coverage (see the enforce-mode flip checklist in the design doc).
CONFIRMED_PATTERN_FAMILIES: list[tuple[str, re.Pattern[str]]] = [
    ("successfully_applied_or_registered", re.compile(
        r"successfully\s*(applied|registered)", re.IGNORECASE)),
    # Broadened against the first real sample (docs/design/10 §Feature 1,
    # 2026-07-11 TCS NQT confirmation): real CDC-adjacent copy inserts a
    # qualifier/test name and adverbs ("...for the TCS National Qualifier
    # Test (NQT) has been successfully submitted") between the anchor word
    # and the verb — the original tight ".{0,0}" adjacency never matched it.
    ("application_or_registration_received", re.compile(
        r"(application|registration)\b.{0,100}?\b"
        r"(received|submitted|confirmed)\b", re.IGNORECASE)),
    ("thank_you_for_applying", re.compile(
        r"thank\s*you\s*for\s*(applying|registering)", re.IGNORECASE)),
    ("you_have_applied", re.compile(
        r"you\s*have\s*applied\s*(for|to)", re.IGNORECASE)),
]

_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Real institutional HTML mail routinely carries hundreds of characters of
# <style>/<script> content before any real text (see the IBM Cloud fixture
# in scripts/eval/corpus/) — strip the element bodies too, not just the
# surrounding tags, or that boilerplate can crowd real content out of the
# truncation window in find_confident_drive_match.
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def _strip_html(text: str) -> str:
    """Tolerate HTML mail bodies (feature_1_spec) before pattern matching."""
    without_style_script = _STYLE_SCRIPT_RE.sub(" ", text or "")
    return _HTML_TAG_RE.sub(" ", without_style_script)


def detect_confirmation_tier(subject: str, body: str = "") -> tuple[str, str | None]:
    """Return (tier, pattern_family_name).

    tier is "CONFIRMED" (a named family matched) or "UNKNOWN" (sender gate
    passed but no family did — still an APPLICATION_CONFIRMATION per the
    sender-is-confirmation-only design, just an unrecognized phrasing; the
    escape valve for formats nobody predicted).
    """
    combined = _strip_html(f"{subject} {body}")
    for family_name, pattern in CONFIRMED_PATTERN_FAMILIES:
        if pattern.search(combined):
            return "CONFIRMED", family_name
    return "UNKNOWN", None


# Generic drive/reference/registration-ID extraction. Per docs/design/08
# blocker 3, this is structurally unlikely to ever match anything today: no
# CDC-side reference/registration number is stored anywhere on
# `opportunities` to compare it against (drive_id is our own internal slug,
# not something CDC would echo back). Implemented per spec anyway — it's
# cheap, and the enforce-mode flip checklist explicitly anticipates adding a
# real reference-number column once a real sample reveals CDC's ID format.
_REFERENCE_ID_RE = re.compile(
    r"(?:drive|ref(?:erence)?|registration)\s*(?:id|no\.?|number)?\s*[:\-#]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-]{2,19})",
    re.IGNORECASE,
)


def extract_reference_id(subject: str, body: str = "") -> str | None:
    """Best-effort drive/ref/registration ID near a labeling word."""
    combined = _strip_html(f"{subject}\n{body}"[:1000])
    match = _REFERENCE_ID_RE.search(combined)
    return match.group(1) if match else None


@dataclass
class ConfirmationMatch:
    """A confident drive match for an application-confirmation mail."""

    opportunity: dict[str, Any]
    method: str  # "reference_id" or "fuzzy_company"
    score: float | None = None


# Conservative fuzzy threshold (feature_1_spec): materially above the 60%
# floor used for sheet display (docs/design/08 C3/blocker 3) — an automatic
# status write needs a much higher, harder-to-notice-if-wrong bar than a
# sheet row a human reviews anyway.
FUZZY_MATCH_THRESHOLD = 90.0
FUZZY_UNIQUENESS_MARGIN = 5.0

# rapidfuzz.fuzz.partial_ratio aligns the shorter string against the best-
# matching substring of the same length in the longer one — for a short
# company name this means ANY coincidental same-length substring scores
# ~100 regardless of word boundaries. Confirmed against the first real
# sample (docs/design/10): "SES" tied at 100 with the real "TCS" match
# because "ses" is a literal substring of "assessments" in the body. Below
# this length, require an actual whole-word match instead of partial_ratio.
_SHORT_NAME_LEN = 4


def find_confident_drive_match(
    subject: str,
    body: str,
    active_opportunities: list[dict[str, Any]],
    *,
    reference_id: str | None = None,
) -> tuple[ConfirmationMatch | None, list[dict[str, Any]]]:
    """Return (confident match or None, all scored candidates).

    The candidate list is returned unconditionally so a caller with no
    confident match can persist it to ``unmatched_confirmations`` for human
    review (docs/design/08 C3) instead of silent-dropping the mail.
    """
    if reference_id:
        for opp in active_opportunities:
            drive_id = opp.get("drive_id")
            if drive_id and drive_id.strip().lower() == reference_id.strip().lower():
                return ConfirmationMatch(opportunity=opp, method="reference_id"), []

    combined = _strip_html(f"{subject} {body}")[:500].casefold()
    scored: list[dict[str, Any]] = []
    for opp in active_opportunities:
        name = opp.get("company_name")
        if not name:
            continue
        normalized = normalize_company_name(str(name))
        if not normalized:
            continue
        normalized_cf = normalized.casefold()
        if len(normalized_cf) <= _SHORT_NAME_LEN:
            pattern = rf"(?<![a-z0-9]){re.escape(normalized_cf)}(?![a-z0-9])"
            score = 100.0 if re.search(pattern, combined) else 0.0
        else:
            score = fuzz.partial_ratio(normalized_cf, combined)
        scored.append(
            {"drive_id": opp.get("drive_id"), "company_name": name, "score": round(score, 1)}
        )

    scored.sort(key=lambda c: c["score"], reverse=True)
    if not scored or scored[0]["score"] < FUZZY_MATCH_THRESHOLD:
        return None, scored
    if len(scored) > 1 and (scored[0]["score"] - scored[1]["score"]) < FUZZY_UNIQUENESS_MARGIN:
        return None, scored  # ambiguous — two candidates too close to call confidently

    best_drive_id = scored[0]["drive_id"]
    best_opp = next(o for o in active_opportunities if o.get("drive_id") == best_drive_id)
    match = ConfirmationMatch(
        opportunity=best_opp, method="fuzzy_company", score=scored[0]["score"]
    )
    return match, scored
