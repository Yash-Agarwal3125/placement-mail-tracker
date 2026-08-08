"""Roster verification: is this student on this drive's shortlist/roster?

docs/design/15 §3 (design), docs/design/16 Phase C (implementation). No
sample roster ever existed in this codebase (checked scripts/eval/corpus and
unmatched_confirmations before writing this), so the matcher checks all
three signal types rather than betting on one format: an exact
registration-number match, an exact Neo-PAT-codename match, and a fuzzy
name match as a fallback only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from placement_mail_tracker.config.user_profile import UserProfile

# Higher than the confirmation-matching thresholds (90.0 / 5.0,
# extraction/confirmation.py) -- a roster contains hundreds of real student
# names, many genuinely similar, so this needs a stricter bar (doc 15 §3.2).
ROSTER_NAME_THRESHOLD = 92.0
ROSTER_UNIQUENESS_MARGIN = 8.0

# VIT-style registration number: two digits, 2-6 letters, 3-5 digits
# (23BAI0011, 22MIS0565 -- both seen in real captured mail).
_REG_NO_RE = re.compile(r"\b\d{2}[A-Z]{2,6}\d{3,5}\b")


def _contains_id(text: str, identifier: str) -> bool:
    """Whole-token match: ``identifier`` must be a distinct alphanumeric
    token in ``text``, not merely a substring of a longer one.

    A plain ``normalized_identifier in normalized_text`` check (an earlier
    version of this function) would match "23BAI0011" inside "1223BAI00119"
    -- a false MATCHED, the worst failure mode this module exists to avoid
    (doc 15 §3.3).
    """
    ident = identifier.strip().upper()
    if not ident:
        return False
    pattern = re.compile(r"(?<![A-Z0-9])" + re.escape(ident) + r"(?![A-Z0-9])")
    return bool(pattern.search(text.upper()))


@dataclass(frozen=True)
class RosterVerdict:
    """One of the three outcomes doc 15 §3.3 allows -- no fourth option."""

    verdict: str  # "MATCHED" | "NOT_MATCHED" | "AMBIGUOUS"
    method: str  # "registration_no" | "codename" | "name_fuzzy" | "none"
    score: float | None = None


def _best_name_score(text: str, full_name: str) -> tuple[float, bool] | None:
    """Fuzzy-match ``full_name`` against every line/token in ``text``.

    Returns (best_score, margin_clear) where margin_clear means the best
    score beat the runner-up by at least ROSTER_UNIQUENESS_MARGIN -- doc
    15's "absolute threshold plus a uniqueness margin" two-part test,
    following the precedent already set in extraction/confirmation.py.
    """
    if not full_name.strip():
        return None
    candidates = [line.strip() for line in re.split(r"[\n,;]", text) if line.strip()]
    if not candidates:
        return None
    scores = sorted(
        (fuzz.token_sort_ratio(full_name, candidate) for candidate in candidates),
        reverse=True,
    )
    best = scores[0]
    runner_up = scores[1] if len(scores) > 1 else 0.0
    return best, (best - runner_up) >= ROSTER_UNIQUENESS_MARGIN


def verify_roster(roster_text: str, profile: UserProfile) -> RosterVerdict:
    """Determine whether ``profile``'s student appears on this roster.

    The hard rule (doc 15 §3.3): AMBIGUOUS must never resolve toward
    MATCHED, and NOT_MATCHED is only assertable when the roster
    demonstrably parsed -- at least one registration number was found on it
    somewhere. A roster that yields zero IDs proves nothing about this
    student's absence; that is AMBIGUOUS, not NOT_MATCHED.
    """
    if not profile.is_identity_verified():
        # Doc 15 §3.1: matching against an unverified identity could assert
        # a shortlist verdict about the wrong person -- refuse outright.
        return RosterVerdict("AMBIGUOUS", "none")

    text = roster_text or ""

    if _contains_id(text, profile.registration_no):
        return RosterVerdict("MATCHED", "registration_no", 100.0)

    for codename in profile.codenames:
        if _contains_id(text, codename):
            return RosterVerdict("MATCHED", "codename", 100.0)

    name_match = _best_name_score(text, profile.full_name)
    if name_match is not None:
        best_score, margin_clear = name_match
        if best_score >= ROSTER_NAME_THRESHOLD and margin_clear:
            return RosterVerdict("MATCHED", "name_fuzzy", best_score)

    if _REG_NO_RE.search(text.upper()):
        return RosterVerdict("NOT_MATCHED", "registration_no", None)

    return RosterVerdict("AMBIGUOUS", "none")
