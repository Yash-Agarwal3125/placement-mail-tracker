"""Intelligent duplicate detection for placement opportunities.

This module provides exact and fuzzy matching to identify duplicate or near-duplicate
placement/internship opportunities.  It compares ``company_name``, ``role``, and
``internship_or_fulltime`` (opportunity type) fields and produces a structured
:class:`DuplicateResult` with per-field similarity scores and an aggregate confidence score.

Typical usage
-------------
::

    from placement_mail_tracker.utils.deduplication import (
        DuplicateResult,
        DeduplicationConfig,
        find_best_match,
        is_duplicate,
        detect_updates,
    )

    config = DeduplicationConfig()

    incoming = {
        "company_name": "Google",
        "role": "SWE Intern",
        "internship_or_fulltime": "internship",
    }
    candidates = [...]  # list[dict] from DatabaseManager.fetch_active_opportunities()

    result = find_best_match(incoming, candidates, config=config)
    if result and result.is_duplicate:
        updates = detect_updates(incoming, result.candidate, config=config)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

try:
    from rapidfuzz import fuzz as _fuzz
    from rapidfuzz import process as _process

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

# Fields considered when detecting *updates* to an already-matched opportunity
UPDATE_FIELDS = (
    "package_or_stipend",
    "eligibility",
    "cgpa_requirement",
    "branches_allowed",
    "deadline",
    "interview_date",
    "oa_date",
    "registration_link",
    "work_location",
    "hiring_process",
    "important_notes",
)


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
}


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
    """Normalise a company name by stripping common legal-entity suffixes and title-casing.

    Example
    -------
    >>> normalize_company("Acme Technologies Pvt. Ltd.")
    'Acme'
    >>> normalize_company("TATA MOTORS")
    'Tata Motors'
    """
    base = normalize_text(text)
    tokens = [t for t in base.split() if t not in _STRIP_TOKENS]
    normalized = " ".join(tokens) if tokens else base
    # Return Title Case for presentation
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


def fuzzy_score_type(a: str | None, b: str | None) -> float:
    """Return fuzzy similarity score for two opportunity-type strings (0–100).

    Uses exact canonical mapping first; falls back to fuzzy on the raw
    normalised strings to handle typos such as "internhsip".
    """
    canon_a = normalize_opportunity_type(a)
    canon_b = normalize_opportunity_type(b)
    if canon_a == canon_b:
        return 100.0
    return _rapidfuzz_ratio(canon_a, canon_b)


# ---------------------------------------------------------------------------
# Per-field score builders
# ---------------------------------------------------------------------------


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
    return FieldScore(
        field_name=field_name,
        incoming_value=norm_inc,
        candidate_value=norm_cnd,
        exact_match=is_exact,
        fuzzy_score=fuzzy,
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
        Weighted average of the three effective scores, rounded to 2 dp.
    """
    weighted = (
        company_score.effective_score * config.company_weight
        + role_score.effective_score * config.role_weight
        + type_score.effective_score * config.type_weight
    )
    return round(weighted, 2)


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

    # Hard gate: type mismatch can disqualify regardless of confidence
    if config.require_type_match:
        type_effective = t_score.effective_score
        if type_effective < config.type_fuzzy_threshold:
            confidence = min(confidence, config.duplicate_confidence_threshold - 0.01)

    is_dup = (
        c_score.effective_score >= config.company_fuzzy_threshold
        and r_score.effective_score >= config.role_fuzzy_threshold
        and confidence >= config.duplicate_confidence_threshold
    )

    is_exact = c_score.exact_match and r_score.exact_match and t_score.exact_match

    return DuplicateResult(
        candidate=candidate,
        company_score=c_score,
        role_score=r_score,
        type_score=t_score,
        confidence_score=confidence,
        is_duplicate=is_dup,
        is_exact=is_exact,
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
    """
    if config is None:
        config = DeduplicationConfig()

    inc_company = normalize_company(incoming.get(FIELD_COMPANY, ""))
    first_char = inc_company[0] if inc_company else ""

    filtered_candidates = []
    for c in candidates:
        cand_company = normalize_company(c.get(FIELD_COMPANY, ""))
        if not first_char or not cand_company or cand_company[0] == first_char:
            filtered_candidates.append(c)

    results = [compare_opportunities(incoming, c, config=config) for c in filtered_candidates]
    duplicates = [r for r in results if r.is_duplicate]
    duplicates.sort(key=lambda r: r.confidence_score, reverse=True)
    return duplicates


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
        Best match (highest confidence) when a duplicate exists, else ``None``.
    """
    matches = find_all_matches(incoming, candidates, config=config)
    if not matches:
        logger.debug(
            "No duplicate found for %s / %s",
            incoming.get(FIELD_COMPANY),
            incoming.get(FIELD_ROLE),
        )
        return None

    best = matches[0]
    logger.info(
        "Duplicate detected for '%s / %s' → candidate id=%s  %s",
        incoming.get(FIELD_COMPANY),
        incoming.get(FIELD_ROLE),
        best.candidate_id,
        best.summary(),
    )
    return best


def is_duplicate(
    incoming: dict[str, Any],
    candidates: list[dict[str, Any]],
    config: DeduplicationConfig | None = None,
) -> bool:
    """Convenience predicate: ``True`` when *incoming* duplicates any candidate.

    Parameters
    ----------
    incoming:
        The new opportunity to check.
    candidates:
        All stored opportunities to compare against.
    config:
        Optional :class:`DeduplicationConfig`.
    """
    return find_best_match(incoming, candidates, config=config) is not None


# ---------------------------------------------------------------------------
# Update detection
# ---------------------------------------------------------------------------


def detect_updates(
    incoming: dict[str, Any],
    candidate: dict[str, Any],
    config: DeduplicationConfig | None = None,  # noqa: ARG001 – reserved for future use
) -> list[UpdatedField]:
    """Detect which non-key fields changed between an incoming record and its duplicate.

    Compares the fields listed in :data:`UPDATE_FIELDS`.  Both sides are
    normalised to empty strings before comparison so that ``None`` and ``""``
    are treated as equivalent.

    Parameters
    ----------
    incoming:
        The new opportunity data.
    candidate:
        The stored opportunity (from the DB) that was matched as a duplicate.
    config:
        Reserved for future threshold-based update rules; currently unused.

    Returns
    -------
    list[UpdatedField]
        Each entry represents a field that has a different value in *incoming*
        versus *candidate*.
    """
    changed: list[UpdatedField] = []

    for field_name in UPDATE_FIELDS:
        old_raw = candidate.get(field_name)
        new_raw = incoming.get(field_name)

        old_norm = _scalar_to_str(old_raw)
        new_norm = _scalar_to_str(new_raw)

        if old_norm != new_norm:
            changed.append(
                UpdatedField(
                    field_name=field_name,
                    old_value=old_raw,
                    new_value=new_raw,
                )
            )
            logger.debug(
                "Field '%s' changed: %r → %r",
                field_name,
                old_raw,
                new_raw,
            )

    return changed


# ---------------------------------------------------------------------------
# Bulk deduplication across a candidate list (useful for DB pre-scan)
# ---------------------------------------------------------------------------


def find_duplicates_in_list(
    opportunities: list[dict[str, Any]],
    config: DeduplicationConfig | None = None,
) -> list[tuple[int, int, DuplicateResult]]:
    """Scan a flat list and return all pairwise duplicate pairs.

    Useful for auditing an existing database for near-duplicates that slipped
    through the hash-based guard.

    Parameters
    ----------
    opportunities:
        List of opportunity dicts to cross-compare.
    config:
        Optional :class:`DeduplicationConfig`.

    Returns
    -------
    list[tuple[int, int, DuplicateResult]]
        Each entry is ``(i, j, result)`` where ``i < j`` are indices into
        *opportunities* and *result* is the comparison outcome.  Only pairs
        flagged as ``is_duplicate`` are included.
    """
    if config is None:
        config = DeduplicationConfig()

    found: list[tuple[int, int, DuplicateResult]] = []
    n = len(opportunities)
    for i in range(n):
        for j in range(i + 1, n):
            result = compare_opportunities(opportunities[i], opportunities[j], config=config)
            if result.is_duplicate:
                found.append((i, j, result))

    return found


# ---------------------------------------------------------------------------
# RapidFuzz-powered candidate pre-filter (fast path for large corpora)
# ---------------------------------------------------------------------------


def rapidfuzz_prefilter(
    incoming_company: str | None,
    candidates: list[dict[str, Any]],
    *,
    limit: int = 10,
    score_cutoff: float = 70.0,
) -> list[dict[str, Any]]:
    """Use RapidFuzz ``process.extract`` to shortlist plausible candidates quickly.

    When the DB is large, comparing every record with full fuzzy logic can be
    expensive.  This function uses RapidFuzz's vectorised C extension to quickly
    shortlist the top *N* candidates by company-name similarity.

    Parameters
    ----------
    incoming_company:
        The company name from the incoming opportunity.
    candidates:
        All stored opportunity dicts.
    limit:
        Maximum number of shortlisted candidates to return.
    score_cutoff:
        Minimum token-set ratio to include a candidate.

    Returns
    -------
    list[dict]
        Shortlisted candidates (subset of *candidates*), ordered by similarity.
        Returns all *candidates* unchanged when RapidFuzz is not installed.
    """
    if not _RAPIDFUZZ_AVAILABLE or not candidates:
        return candidates

    query = normalize_company(incoming_company)
    choices = [normalize_company(c.get(FIELD_COMPANY)) for c in candidates]

    hits = _process.extract(
        query,
        choices,
        scorer=_fuzz.token_set_ratio,
        limit=limit,
        score_cutoff=score_cutoff,
    )

    # hits → list of (match_str, score, index)
    indices = {hit[2] for hit in hits}
    return [candidates[i] for i in sorted(indices)]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _scalar_to_str(value: Any) -> str:
    """Coerce a scalar DB value to a normalised string for comparison."""
    if value is None:
        return ""
    if isinstance(value, list):
        return normalize_text(", ".join(str(v) for v in value))
    return normalize_text(str(value))
