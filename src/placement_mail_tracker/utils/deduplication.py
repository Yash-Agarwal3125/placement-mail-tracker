"""Intelligent duplicate detection for placement opportunities.

Provides exact and fuzzy matching to identify duplicate or near-duplicate
placement/internship opportunities. Compares ``company_name``, ``role``, and
``internship_or_fulltime`` and produces a structured :class:`DuplicateResult`.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from placement_mail_tracker.extraction.rule_engine import normalize_company_name
from placement_mail_tracker.utils.time import parse_datetime_flexible

try:
    from rapidfuzz import fuzz as _fuzz

    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover – optional dependency
    _RAPIDFUZZ_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public field names that mirror the opportunities DB schema
# ---------------------------------------------------------------------------

FIELD_COMPANY = "company_name"
FIELD_ROLE = "role"
FIELD_TYPE = "internship_or_fulltime"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class DeduplicationConfig:
    """Configurable thresholds and weights for duplicate detection.

    Attributes
    ----------
    exact_match_threshold:
        Normalised-string equality check threshold (0-100).  A value of 100
        means the strings must be identical after normalisation.
    company_fuzzy_threshold:
        Minimum RapidFuzz similarity (0-100) for ``company_name`` to be
        considered a potential match.
    role_fuzzy_threshold:
        Minimum RapidFuzz similarity (0-100) for ``role`` to be a match.
    type_fuzzy_threshold:
        Minimum RapidFuzz similarity (0-100) for ``internship_or_fulltime``.
    duplicate_confidence_threshold:
        Minimum *weighted aggregate score* (0-100) required for a pair to be
        flagged as a duplicate.
    company_weight:
        Relative weight of company similarity in the aggregate score.
    role_weight:
        Relative weight of role similarity.
    type_weight:
        Relative weight of opportunity-type similarity.
    require_type_match:
        When ``True`` an internship and a full-time role at the same company
        are **never** considered duplicates, regardless of other scores.
    """

    exact_match_threshold: float = 100.0
    company_fuzzy_threshold: float = 85.0
    role_fuzzy_threshold: float = 80.0
    type_fuzzy_threshold: float = 75.0
    duplicate_confidence_threshold: float = 82.0

    company_weight: float = 0.45
    role_weight: float = 0.40
    type_weight: float = 0.15

    require_type_match: bool = True

    # Doc 15 §1.4-D: event-coincidence tolerance used when neither side shares
    # a source_thread_id and no date field matches to the exact minute.
    event_coincidence_tolerance_hours: float = 6.0

    def __post_init__(self) -> None:
        total = self.company_weight + self.role_weight + self.type_weight
        if abs(total - 1.0) > 1e-6:
            msg = (
                f"Weights must sum to 1.0; got {total:.4f}.  "
                "Adjust company_weight, role_weight, and type_weight."
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class FieldScore:
    """Similarity score for a single compared field.

    Attributes
    ----------
    field_name: Name of the compared field.
    incoming_value: The raw (post-normalisation) value from the incoming record.
    candidate_value: The raw (post-normalisation) value from the stored candidate.
    exact_match: Whether the values match exactly after normalisation.
    fuzzy_score: RapidFuzz similarity score (0–100).  ``None`` when RapidFuzz is
        not installed and the field did not exact-match.
    """

    field_name: str
    incoming_value: str
    candidate_value: str
    exact_match: bool
    fuzzy_score: float | None = None
    is_absent: bool = False
    """``True`` when either side's normalised value is missing/empty, or the
    role sentinel ``"unknown role"``. Doc 15 §1.4-A: an absent value must
    never be scored as a mismatch — the field is dropped from the aggregate
    and its weight redistributed."""

    @property
    def effective_score(self) -> float:
        """Return 100.0 for exact matches, ``fuzzy_score`` otherwise."""
        if self.exact_match:
            return 100.0
        return self.fuzzy_score if self.fuzzy_score is not None else 0.0


@dataclass
class DuplicateResult:
    """Full duplicate detection result for a single (incoming, candidate) pair.

    Attributes
    ----------
    candidate: The stored opportunity dict that was compared.
    company_score: Per-field score for ``company_name``.
    role_score: Per-field score for ``role``.
    type_score: Per-field score for ``internship_or_fulltime``.
    confidence_score: Weighted aggregate score in [0, 100].
    is_duplicate: ``True`` when *confidence_score* exceeds the configured threshold.
    is_exact: ``True`` when all three key fields are exact matches.
    """

    candidate: dict[str, Any]
    company_score: FieldScore
    role_score: FieldScore
    type_score: FieldScore
    confidence_score: float
    is_duplicate: bool
    is_exact: bool
    updated_fields: list[UpdatedField] = field(default_factory=list)
    review_required: bool = False
    """Doc 15 §1.4-D: identity (company+role/type) matched well enough to be
    suspicious, but no event coincidence (shared thread / matching date) was
    found and no blocker fired either. Route to ``unmatched_confirmations``
    for human review instead of silently inserting or silently merging."""
    blocked_reason: str | None = None
    """Set when a hard blocker (type conflict, role conflict, eligibility
    conflict, non-overlapping dates) prevented a merge that the raw
    confidence score alone would otherwise have allowed."""

    @property
    def candidate_id(self) -> int | None:
        """Return the DB primary key of the matched candidate, if present."""
        return self.candidate.get("id")  # type: ignore[return-value]

    def summary(self) -> str:
        """Return a human-readable one-line summary of the match."""
        kind = "EXACT" if self.is_exact else "FUZZY"
        dup = "DUPLICATE" if self.is_duplicate else "UNIQUE"
        return (
            f"[{dup}/{kind}] confidence={self.confidence_score:.1f}  "
            f"company={self.company_score.effective_score:.1f}  "
            f"role={self.role_score.effective_score:.1f}  "
            f"type={self.type_score.effective_score:.1f}"
        )


@dataclass
class UpdatedField:
    """A single field that differs between an incoming record and its stored duplicate.

    Attributes
    ----------
    field_name: The DB column name.
    old_value: The value currently stored in the DB.
    new_value: The value from the incoming record.
    """

    field_name: str
    old_value: Any
    new_value: Any


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


# Common legal-entity suffixes and filler words to strip before comparison
_STRIP_TOKENS = frozenset(
    {
        "inc",
        "inc.",
        "ltd",
        "ltd.",
        "llc",
        "llp",
        "pvt",
        "pvt.",
        "private",
        "limited",
        "technologies",
        "technology",
        "tech",
        "solutions",
        "services",
        "systems",
        "global",
        "india",
        "corp",
        "corp.",
        "corporation",
        "group",
        "holdings",
        "enterprises",
    }
)

# Canonical synonyms for opportunity-type normalisation
_TYPE_SYNONYMS: dict[str, str] = {
    "intern": "internship",
    "internship": "internship",
    "summer intern": "internship",
    "summer internship": "internship",
    "fte": "full_time",
    "full time": "full_time",
    "full-time": "full_time",
    "fulltime": "full_time",
    "permanent": "full_time",
    "ppo": "full_time",
    "pre placement offer": "full_time",
    "pre-placement offer": "full_time",
    "contract": "contract",
    "freelance": "contract",
    "part time": "part_time",
    "part-time": "part_time",
    "internship_and_fulltime": "internship_and_fulltime",
    "internship and fulltime": "internship_and_fulltime",
    "internship and full time": "internship_and_fulltime",
    "internship full time": "internship_and_fulltime",
}

# Canonical opportunity types that are considered directly comparable.
# ``internship_and_fulltime`` is compatible with *both* — never a third,
# mutually-exclusive value (doc 15 §1.4-D).
_KNOWN_TYPES = frozenset({"internship", "full_time", "internship_and_fulltime"})


def _unicode_normalize(text: str) -> str:
    """Apply NFKC unicode normalisation and strip leading/trailing whitespace."""
    return unicodedata.normalize("NFKC", text).strip()


def _remove_punctuation(text: str) -> str:
    """Replace all non-alphanumeric, non-space chars with a space."""
    return re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)


def normalize_text(text: str | None) -> str:
    """Return a canonical lowercase, stripped, punctuation-free string.

    Returns an empty string when *text* is ``None`` or blank.
    """
    if not text:
        return ""
    text = _unicode_normalize(text)
    text = text.casefold()
    text = _remove_punctuation(text)
    # collapse whitespace
    return " ".join(text.split())


def normalize_company(text: str | None) -> str:
    """Normalise a company name for duplicate matching.

    Doc 15 §1.4-C: this used to duplicate a weaker suffix-stripping routine
    that never handled label-colon prefixes ("Update:", "Join Immediately:")
    or alias canonicalisation. It now routes through
    :func:`rule_engine.normalize_company_name`, the richer implementation
    already used at extraction time, so the two layers agree on identity.

    Example
    -------
    >>> normalize_company("Acme Technologies Pvt. Ltd.")
    'Acme'
    >>> normalize_company("Update: Varroc Engineering")
    'Varroc Engineering'
    """
    normalized = normalize_company_name(text)
    if normalized:
        return normalized
    # Fall back to the legacy suffix-stripping path only when rule_engine
    # returns nothing usable (e.g. pure punctuation/garbage input).
    base = normalize_text(text)
    tokens = [t for t in base.split() if t not in _STRIP_TOKENS]
    normalized = " ".join(tokens) if tokens else base
    return normalized.title()


def normalize_opportunity_type(text: str | None) -> str:
    """Map raw opportunity-type strings to a small canonical vocabulary.

    Returns one of ``"internship"``, ``"full_time"``, ``"contract"``,
    ``"part_time"``, or the normalised raw value when no synonym matches.
    """
    base = normalize_text(text)
    return _TYPE_SYNONYMS.get(base, base)


# ---------------------------------------------------------------------------
# Exact matching
# ---------------------------------------------------------------------------


def exact_match(a: str, b: str) -> bool:
    """Return ``True`` when two normalised strings are identical."""
    return a == b


def exact_match_fields(
    incoming: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, bool, bool]:
    """Check exact equality for (company, role, type) after normalisation.

    Returns
    -------
    tuple[bool, bool, bool]
        ``(company_exact, role_exact, type_exact)``
    """
    company_exact = exact_match(
        normalize_company(incoming.get(FIELD_COMPANY)),
        normalize_company(candidate.get(FIELD_COMPANY)),
    )
    role_exact = exact_match(
        normalize_text(incoming.get(FIELD_ROLE)),
        normalize_text(candidate.get(FIELD_ROLE)),
    )
    type_exact = exact_match(
        normalize_opportunity_type(incoming.get(FIELD_TYPE)),
        normalize_opportunity_type(candidate.get(FIELD_TYPE)),
    )
    return company_exact, role_exact, type_exact


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------


def _rapidfuzz_ratio(a: str, b: str) -> float:
    """Compute a RapidFuzz partial token-set ratio in [0, 100].

    Uses ``token_set_ratio`` which is robust to word-order differences and
    substring relationships (e.g. "Google" vs "Google LLC").

    Falls back to 0.0 when RapidFuzz is not installed.
    """
    if not _RAPIDFUZZ_AVAILABLE:
        logger.debug("rapidfuzz not installed – fuzzy scoring unavailable")
        return 0.0
    return float(_fuzz.token_set_ratio(a, b))


def fuzzy_score_company(a: str | None, b: str | None) -> float:
    """Return fuzzy similarity score for two company names (0–100)."""
    return _rapidfuzz_ratio(normalize_company(a), normalize_company(b))


def fuzzy_score_role(a: str | None, b: str | None) -> float:
    """Return fuzzy similarity score for two role titles (0–100)."""
    return _rapidfuzz_ratio(normalize_text(a), normalize_text(b))


def types_compatible(canon_a: str, canon_b: str) -> bool:
    """Return ``True`` when two *canonical* opportunity types may co-exist.

    ``internship_and_fulltime`` is compatible with both ``internship`` and
    ``full_time`` (and with itself) — it is not a third, mutually-exclusive
    value (doc 15 §1.4-D). Unknown/empty values are handled separately by the
    absent-field logic and are not this function's concern.
    """
    if not canon_a or not canon_b:
        return True
    if canon_a == canon_b:
        return True
    if "internship_and_fulltime" in (canon_a, canon_b):
        other = canon_b if canon_a == "internship_and_fulltime" else canon_a
        return other in _KNOWN_TYPES
    return False


def fuzzy_score_type(a: str | None, b: str | None) -> float:
    """Return fuzzy similarity score for two opportunity-type strings (0–100).

    Uses exact canonical mapping first, then the compatibility rule for
    ``internship_and_fulltime``, then falls back to fuzzy matching on the raw
    normalised strings to handle typos such as "internhsip".
    """
    canon_a = normalize_opportunity_type(a)
    canon_b = normalize_opportunity_type(b)
    if canon_a == canon_b:
        return 100.0
    if types_compatible(canon_a, canon_b):
        return 100.0
    return _rapidfuzz_ratio(canon_a, canon_b)


# ---------------------------------------------------------------------------
# Per-field score builders
# ---------------------------------------------------------------------------


# Sentinel normalised values that mean "no real data was extracted here" per
# field. Doc 15 §1.4-A: a value in this set on *either* side means the field
# must be dropped from the comparison, never scored as a mismatch.
_ABSENT_SENTINELS: dict[str, frozenset[str]] = {
    FIELD_COMPANY: frozenset({""}),
    FIELD_ROLE: frozenset({"", "unknown role"}),
    FIELD_TYPE: frozenset({""}),
}


def _score_field(
    field_name: str,
    incoming_raw: str | None,
    candidate_raw: str | None,
    *,
    normalise_fn: Any,
    fuzzy_fn: Any,
) -> FieldScore:
    """Build a :class:`FieldScore` for one field."""
    norm_inc = normalise_fn(incoming_raw)
    norm_cnd = normalise_fn(candidate_raw)
    is_exact = exact_match(norm_inc, norm_cnd)
    fuzzy = fuzzy_fn(incoming_raw, candidate_raw) if not is_exact else None
    sentinels = _ABSENT_SENTINELS.get(field_name, frozenset({""}))
    is_absent = norm_inc in sentinels or norm_cnd in sentinels
    return FieldScore(
        field_name=field_name,
        incoming_value=norm_inc,
        candidate_value=norm_cnd,
        exact_match=is_exact,
        fuzzy_score=fuzzy,
        is_absent=is_absent,
    )


def score_company(incoming: dict[str, Any], candidate: dict[str, Any]) -> FieldScore:
    """Return a :class:`FieldScore` for the ``company_name`` field."""
    return _score_field(
        FIELD_COMPANY,
        incoming.get(FIELD_COMPANY),
        candidate.get(FIELD_COMPANY),
        normalise_fn=normalize_company,
        fuzzy_fn=fuzzy_score_company,
    )


def score_role(incoming: dict[str, Any], candidate: dict[str, Any]) -> FieldScore:
    """Return a :class:`FieldScore` for the ``role`` field."""
    return _score_field(
        FIELD_ROLE,
        incoming.get(FIELD_ROLE),
        candidate.get(FIELD_ROLE),
        normalise_fn=normalize_text,
        fuzzy_fn=fuzzy_score_role,
    )


def score_type(incoming: dict[str, Any], candidate: dict[str, Any]) -> FieldScore:
    """Return a :class:`FieldScore` for the ``internship_or_fulltime`` field."""
    return _score_field(
        FIELD_TYPE,
        incoming.get(FIELD_TYPE),
        candidate.get(FIELD_TYPE),
        normalise_fn=normalize_opportunity_type,
        fuzzy_fn=fuzzy_score_type,
    )


# ---------------------------------------------------------------------------
# Confidence / aggregate scoring
# ---------------------------------------------------------------------------


def compute_confidence_score(
    company_score: FieldScore,
    role_score: FieldScore,
    type_score: FieldScore,
    config: DeduplicationConfig,
) -> float:
    """Compute the weighted aggregate confidence score in [0, 100].

    Parameters
    ----------
    company_score, role_score, type_score:
        Per-field :class:`FieldScore` objects.
    config:
        :class:`DeduplicationConfig` carrying the field weights.

    Returns
    -------
    float
        Weighted average of the effective scores over the fields that
        actually have data on both sides, rounded to 2 dp.

    Doc 15 §1.4-A: a field where either side is absent/empty (or the role
    sentinel ``"unknown role"``) is dropped entirely rather than scored as a
    0.0 mismatch, and its weight is redistributed across the remaining
    fields. If every field is absent there is nothing to compare, so the
    result is 0.0.
    """
    candidates = (
        (company_score, config.company_weight),
        (role_score, config.role_weight),
        (type_score, config.type_weight),
    )
    applicable = [(score, weight) for score, weight in candidates if not score.is_absent]
    if not applicable:
        return 0.0

    total_weight = sum(weight for _, weight in applicable)
    weighted = sum(score.effective_score * weight for score, weight in applicable)
    return round(weighted / total_weight, 2)


# ---------------------------------------------------------------------------
# Doc 15 §1.4-D: blockers and event coincidence
#
# These implement the two-stage discriminating key: (1) identity — canonical
# company + role/type similarity, necessary but never sufficient — and
# (2) event coincidence, required to actually merge. A handful of hard
# blockers can veto a merge even when identity and coincidence both pass.
# ---------------------------------------------------------------------------


def _type_conflict(type_score: FieldScore) -> bool:
    """``True`` when both sides have a known, genuinely incompatible type.

    Absent-vs-present never conflicts (handled by ``is_absent``);
    ``internship_and_fulltime`` never conflicts with ``internship`` or
    ``full_time``.
    """
    if type_score.is_absent:
        return False
    return not types_compatible(type_score.incoming_value, type_score.candidate_value)


def _role_conflict(role_score: FieldScore, config: DeduplicationConfig) -> bool:
    """``True`` when both sides name a specific, different, non-placeholder role."""
    if role_score.is_absent:
        return False
    return role_score.effective_score < config.role_fuzzy_threshold


def _parse_branch_set(value: Any) -> set[str]:
    """Best-effort parse of a ``branches_allowed`` value into a lowercase set."""
    if isinstance(value, (list, tuple, set)):
        items = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return set()
        items = parsed if isinstance(parsed, list) else []
    else:
        return set()
    return {str(item).strip().lower() for item in items if str(item).strip()}


def _eligibility_conflict(incoming: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """``True`` when both records have disjoint populated eligibility criteria.

    Checks ``degree_level``/``batch`` for equality and ``branches_allowed``
    for set overlap, only when *both* sides have a populated (non-UNKNOWN)
    value — an unpopulated side must never block a merge.
    """
    for key in ("degree_level", "batch"):
        a = incoming.get(key)
        b = candidate.get(key)
        if not a or not b:
            continue
        if str(a).strip().upper() in ("", "UNKNOWN") or str(b).strip().upper() in (
            "",
            "UNKNOWN",
        ):
            continue
        if str(a).strip().casefold() != str(b).strip().casefold():
            return True

    branches_a = _parse_branch_set(incoming.get("branches_allowed"))
    branches_b = _parse_branch_set(candidate.get("branches_allowed"))
    if branches_a and branches_b and branches_a.isdisjoint(branches_b):
        return True

    return False


_EVENT_DATE_FIELDS = ("oa_date", "interview_date", "deadline")


def has_event_coincidence(
    incoming: dict[str, Any],
    candidate: dict[str, Any],
    config: DeduplicationConfig | None = None,
) -> bool:
    """Doc 15 §1.4-D stage 2: do these two records describe the same event?

    ``True`` when either side holds:

    - a shared, non-empty ``source_thread_id``, or
    - any populated date field (``oa_date``/``interview_date``/``deadline``)
      that matches the other side to the minute, or
    - a populated date field within
      ``config.event_coincidence_tolerance_hours`` of the other side.
    """
    if config is None:
        config = DeduplicationConfig()

    thread_a = incoming.get("source_thread_id")
    thread_b = candidate.get("source_thread_id")
    if thread_a and thread_b and thread_a == thread_b:
        return True

    for date_field in _EVENT_DATE_FIELDS:
        raw_a = incoming.get(date_field)
        if not raw_a:
            continue
        raw_b = candidate.get(date_field)
        if not raw_b:
            # The candidate has no value yet for this specific field (e.g.
            # the very first OA notice for a drive that only had a
            # deadline so far) -- a first-time fill can't contradict a date
            # that doesn't exist, so it must not be blocked behind an
            # impossible date match or a shared Gmail thread. Without this,
            # any drive that already has ANY date populated (nearly all of
            # them, via `deadline`) can never accept its first OA/interview
            # date unless the notice happens to land in the same thread as
            # the original announcement -- which VIT CDC mail rarely does.
            return True
        dt_a = parse_datetime_flexible(raw_a)
        dt_b = parse_datetime_flexible(raw_b)
        if dt_a is None or dt_b is None:
            continue
        diff_hours = abs((dt_a - dt_b).total_seconds()) / 3600.0
        if diff_hours <= config.event_coincidence_tolerance_hours:
            return True
        # docs/design/16 Phase 3: a reminder/follow-up mail restating an
        # already-known date often drops the time-of-day (parses to
        # midnight) while the stored value carries a real time — e.g.
        # "4 August 2026" (-> 00:00) vs a stored "2026-08-04T10:00" is a
        # 10-hour gap under the tolerance above, even though it is plainly
        # the same event. Same calendar day is always coincident; a genuine
        # reschedule lands on a different day.
        if dt_a.date() == dt_b.date():
            return True

    return False


def _has_any_populated_date(record: dict[str, Any]) -> bool:
    return any(record.get(f) for f in _EVENT_DATE_FIELDS)


# ---------------------------------------------------------------------------
# Pairwise comparison
# ---------------------------------------------------------------------------


def compare_opportunities(
    incoming: dict[str, Any],
    candidate: dict[str, Any],
    config: DeduplicationConfig | None = None,
) -> DuplicateResult:
    """Compare two opportunity dicts and return a full :class:`DuplicateResult`.

    Parameters
    ----------
    incoming:
        The new opportunity extracted from an email (not yet in the DB).
    candidate:
        A stored opportunity dict (e.g. from :meth:`DatabaseManager.fetch_active_opportunities`).
    config:
        Optional :class:`DeduplicationConfig`; defaults are used when omitted.

    Returns
    -------
    DuplicateResult
        Full comparison result including per-field scores, aggregate confidence,
        and a boolean ``is_duplicate`` flag.
    """
    if config is None:
        config = DeduplicationConfig()

    c_score = score_company(incoming, candidate)
    r_score = score_role(incoming, candidate)
    t_score = score_type(incoming, candidate)

    confidence = compute_confidence_score(c_score, r_score, t_score, config)

    # Hard gate (doc 15 §1.4-A): a type mismatch can disqualify regardless of
    # confidence, but *only* when both sides have a known, incompatible type.
    # An absent type on either side must never trip this gate.
    type_conflict = config.require_type_match and _type_conflict(t_score)
    if type_conflict:
        confidence = min(confidence, config.duplicate_confidence_threshold - 0.01)

    role_conflict = _role_conflict(r_score, config)
    eligibility_conflict = _eligibility_conflict(incoming, candidate)
    blocked = type_conflict or role_conflict or eligibility_conflict

    blocked_reason: str | None = None
    if type_conflict:
        blocked_reason = "type_conflict"
    elif role_conflict:
        blocked_reason = "role_conflict"
    elif eligibility_conflict:
        blocked_reason = "eligibility_conflict"

    # A field that was dropped as absent must not gate is_duplicate on its
    # (meaningless) threshold — that is precisely the "missing value voted"
    # bug this fix removes.
    company_ok = c_score.is_absent or c_score.effective_score >= config.company_fuzzy_threshold
    role_ok = r_score.is_absent or r_score.effective_score >= config.role_fuzzy_threshold
    identity_ok = company_ok and role_ok and confidence >= config.duplicate_confidence_threshold

    # Doc 15 §1.4-D stage 2: identity alone is never sufficient. Require
    # event coincidence (shared thread, or a date match/near-match) before
    # actually merging. But this check only makes sense when the *incoming*
    # mail actually introduces a date to contradict something with. Status-only
    # mail (a rejection notice, an "you're eligible" confirmation, a
    # registration confirmation) never carries oa_date/interview_date/deadline
    # -- it just states a status against an already-known company+role. Such
    # mail has nothing to contradict, no matter how many dates the existing
    # drive already has (nearly all of them have at least a deadline from
    # the original announcement). Gating on the candidate's dates here was
    # the actual reason rejection/status-update mail got silently parked in
    # unmatched_confirmations forever instead of updating current_status.
    coincidence = has_event_coincidence(incoming, candidate, config)
    no_date_evidence = not _has_any_populated_date(incoming)
    coincidence_satisfied = coincidence or no_date_evidence

    is_dup = identity_ok and not blocked and coincidence_satisfied
    review_required = identity_ok and not blocked and not coincidence_satisfied

    is_exact = c_score.exact_match and r_score.exact_match and t_score.exact_match

    return DuplicateResult(
        candidate=candidate,
        company_score=c_score,
        role_score=r_score,
        type_score=t_score,
        confidence_score=confidence,
        is_duplicate=is_dup,
        is_exact=is_exact,
        review_required=review_required,
        blocked_reason=blocked_reason,
    )


# ---------------------------------------------------------------------------
# Batch scanning helpers
# ---------------------------------------------------------------------------


def find_all_matches(
    incoming: dict[str, Any],
    candidates: list[dict[str, Any]],
    config: DeduplicationConfig | None = None,
) -> list[DuplicateResult]:
    """Return all candidates flagged as duplicates, sorted by descending confidence.

    Parameters
    ----------
    incoming:
        The new opportunity to check.
    candidates:
        All stored opportunities to compare against.
    config:
        Optional :class:`DeduplicationConfig`.

    Returns
    -------
    list[DuplicateResult]
        Only results where ``is_duplicate`` is ``True``, highest confidence first.

    Note
    ----
    Doc 15 §1.4-C: this used to pre-filter candidates to those sharing the
    first character of the normalised company name — at 115 rows that is a
    correctness bug, not an optimisation (it permanently prevents e.g.
    "Eternal (Zomato)" [E] from ever being compared against "Zomato" [Z]).
    The prefilter has been removed; every candidate is compared.
    """
    if config is None:
        config = DeduplicationConfig()

    results = [compare_opportunities(incoming, c, config=config) for c in candidates]
    duplicates = [r for r in results if r.is_duplicate]
    duplicates.sort(key=lambda r: r.confidence_score, reverse=True)
    return duplicates


def find_review_matches(
    incoming: dict[str, Any],
    candidates: list[dict[str, Any]],
    config: DeduplicationConfig | None = None,
) -> list[DuplicateResult]:
    """Return candidates that matched on identity but lack event coincidence.

    Doc 15 §1.4-D: a pair that passes identity (company/role/type) but has
    neither a shared thread nor a matching/near-matching date, and is not
    vetoed by a hard blocker, must not be silently merged *or* silently
    inserted as a new drive. It is surfaced here so the caller can route it
    to ``unmatched_confirmations`` for human review.
    """
    if config is None:
        config = DeduplicationConfig()

    results = [compare_opportunities(incoming, c, config=config) for c in candidates]
    reviews = [r for r in results if r.review_required]
    reviews.sort(key=lambda r: r.confidence_score, reverse=True)
    return reviews


def find_best_match(
    incoming: dict[str, Any],
    candidates: list[dict[str, Any]],
    config: DeduplicationConfig | None = None,
) -> DuplicateResult | None:
    """Return the single best-matching duplicate, or ``None`` if no duplicate found.

    This is the primary entry-point for the pipeline: pass in the extracted
    opportunity and all active DB records; the function returns the top hit.

    Parameters
    ----------
    incoming:
        The new opportunity to check.
    candidates:
        All stored opportunities to compare against.
    config:
        Optional :class:`DeduplicationConfig`.

    Returns
    -------
    DuplicateResult | None
        Best duplicate match (``is_duplicate=True``) when one exists;
        otherwise the best *review* match (``review_required=True``, doc 15
        §1.4-D) when identity matched without event coincidence; otherwise
        ``None``. Callers must check ``is_duplicate`` vs ``review_required``
        — they are mutually exclusive on the returned result.
    """
    matches = find_all_matches(incoming, candidates, config=config)
    if matches:
        best = matches[0]
        logger.info(
            "Duplicate detected for '%s / %s' → candidate id=%s  %s",
            incoming.get(FIELD_COMPANY),
            incoming.get(FIELD_ROLE),
            best.candidate_id,
            best.summary(),
        )
        return best

    reviews = find_review_matches(incoming, candidates, config=config)
    if reviews:
        best_review = reviews[0]
        logger.info(
            "Identity match without event coincidence for '%s / %s' → "
            "candidate id=%s  %s (routing to review)",
            incoming.get(FIELD_COMPANY),
            incoming.get(FIELD_ROLE),
            best_review.candidate_id,
            best_review.summary(),
        )
        return best_review

    logger.debug(
        "No duplicate found for %s / %s",
        incoming.get(FIELD_COMPANY),
        incoming.get(FIELD_ROLE),
    )
    return None


