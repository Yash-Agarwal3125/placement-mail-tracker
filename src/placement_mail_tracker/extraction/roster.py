"""Roster verification: is this student on this drive's shortlist/roster?

docs/design/15 §3 (design), docs/design/16 Phase C (implementation). No
sample roster existed in this codebase when this module was first written
(checked scripts/eval/corpus and unmatched_confirmations), so it checked all
three signal types rather than betting on one format: an exact
registration-number match, an exact Neo-PAT-codename match, and a fuzzy
name match as a fallback only.

Verified against 5 real production rosters fetched live from Gmail
(Amazon, Blackrock, Honeywell, ABSYZ, Tekion): every one is a Neo-ID-only
list -- no registration numbers, no names. That confirmed the codename path
is the primary signal in practice, not a fallback, and exposed a real bug
in the original "did the roster parse" check (see verify_roster's
docstring) -- fixed here.
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

# VIT's Neo PAT codename: 4 alternating letter-digit pairs (X2A6L2T3,
# N2Z3K8G4 -- the actual, and apparently exclusive, format of every real
# shortlist roster this module was tested against; see verify_roster's
# docstring). Real production rosters carry Neo IDs, not registration
# numbers, so this -- not _REG_NO_RE -- is what usually proves a roster
# parsed cleanly.
_NEO_ID_RE = re.compile(r"\b(?:[A-Z]\d){4}\b")


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
    """One of four outcomes (docs/design/16 Cause 3 extends doc 15 §3.3's
    original three): AMBIGUOUS now means specifically "roster-shaped content
    was found and evaluated, but the match was uncertain" -- kept because it
    genuinely might be the student. NO_ROSTER means nothing roster-shaped was
    found at all (empty text, an unreadable attachment, or a roster with no
    IDs and no name-worthy match); it must fall back to ordinary eligibility
    gating rather than acting as a free pass. Precedence:
    MATCHED > NOT_MATCHED > AMBIGUOUS > NO_ROSTER, non-downgrading -- see
    ``db.manager.upsert_roster_verdict``.
    """

    verdict: str  # "MATCHED" | "NOT_MATCHED" | "AMBIGUOUS" | "NO_ROSTER"
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

    The hard rule (doc 15 §3.3): AMBIGUOUS/NO_ROSTER must never resolve
    toward MATCHED, and NOT_MATCHED is only assertable when the roster
    demonstrably parsed -- at least one registration number *or* Neo ID was
    found on it somewhere. A roster that yields zero IDs of either kind
    proves nothing about this student's absence; that is AMBIGUOUS or
    NO_ROSTER, not NOT_MATCHED. (Real VIT rosters checked while building
    this were exclusively Neo-ID lists -- registration numbers never
    actually appeared -- so treating only _REG_NO_RE as "the roster parsed"
    silently downgraded every real NOT_MATCHED case to AMBIGUOUS; fixed by
    checking both.)

    docs/design/16 Cause 3 further splits the old catch-all AMBIGUOUS: when
    no ID is found but a name-worthy candidate came close (cleared the score
    threshold without a clear uniqueness margin), that is AMBIGUOUS -- the
    roster genuinely parsed and this genuinely might be the student. When
    nothing roster-shaped was found at all (empty text, an unreadable
    attachment, or no name worth reporting), that is NO_ROSTER -- absence of
    evidence, not evidence of anything, and callers must not treat it as a
    free pass.
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

    upper_text = text.upper()
    if _REG_NO_RE.search(upper_text):
        return RosterVerdict("NOT_MATCHED", "registration_no", None)
    if _NEO_ID_RE.search(upper_text):
        return RosterVerdict("NOT_MATCHED", "codename", None)

    # The roster demonstrably parsed (it had at least one name-worthy line to
    # compare against) and the name match cleared the threshold but not the
    # uniqueness margin -- two similarly-named candidates. That is a genuine
    # "might be you" ambiguity, not an absence of evidence (docs/design/16
    # Cause 3): keep it AMBIGUOUS, distinct from the no-roster-at-all case
    # below.
    if name_match is not None:
        best_score, _margin_clear = name_match
        if best_score >= ROSTER_NAME_THRESHOLD:
            return RosterVerdict("AMBIGUOUS", "name_fuzzy", best_score)

    # Nothing roster-shaped was found at all -- no registration number, no
    # Neo ID, and no name match worth reporting. This proves nothing about
    # the student's presence/absence and must not be treated as "might be
    # you" (docs/design/16 Cause 3).
    return RosterVerdict("NO_ROSTER", "none")
