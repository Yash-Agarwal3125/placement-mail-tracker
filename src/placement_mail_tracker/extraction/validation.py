"""Post-extraction plausibility validation (fail-soft review flags).

This module never blocks storage and never raises: every check here answers
"does this look implausible enough that a human should double-check it?",
not "is this valid enough to keep." Per CLAUDE.md's fail-soft principle, a
flagged drive is written exactly like any other drive — the flags just ride
along in ``validation_flags`` so the sheet can surface a "Needs review" style
signal instead of presenting a guess as fact.

Why a new module instead of folding this into ``eligibility.py``:
``eligibility_status`` answers a different question (does this drive match
*this user's* branch/degree/CGPA?) and is stored as a single sticky enum
(COALESCE'd on every update — db/manager.py `_update_opportunity_row`). A
drive can simultaneously be NOT_ELIGIBLE_CGPA *and* have an implausible OA
date; a single enum column cannot hold both, so validation flags get their
own column (`validation_flags`, a JSON list — see db/manager.py JSON_FIELDS)
rather than overloading `eligibility_status`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from placement_mail_tracker.utils.time import parse_datetime_flexible, parse_datetime_strict

# Below this self-reported confidence, flag the extraction for review even
# though nothing else about it looks structurally wrong.
LOW_CONFIDENCE_THRESHOLD = 0.5

_EVENT_FIELDS = ("deadline", "oa_date", "interview_date")

_CGPA_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def validate_opportunity_data(
    opp_data: dict[str, Any],
    *,
    email_received_at: str | None = None,
) -> list[str]:
    """Return human-readable review flags for implausible extracted values.

    Never raises. An empty list means nothing looked wrong; a non-empty list
    means the drive should still be stored (never dropped), just marked for
    the user to double-check via the caller's ``validation_flags`` column.

    ``email_received_at`` anchors the past-dated-event check to *when the
    mail arrived*, not wall-clock "now" — a backlog batch run processing
    week-old mail must not flag every deadline in it as "in the past".
    """
    flags: list[str] = []

    now_anchor = None
    if email_received_at:
        now_anchor = parse_datetime_flexible(str(email_received_at))
    if now_anchor is None:
        now_anchor = datetime.now()

    flexible_parsed: dict[str, datetime] = {}

    for field_name in _EVENT_FIELDS:
        raw = opp_data.get(field_name)
        if not raw:
            continue
        raw_str = str(raw)

        flexible_dt = parse_datetime_flexible(raw_str)
        if flexible_dt is None:
            # Entirely unparseable is an existing, separately-logged data
            # quality warning (scheduler/runner.py `_warn_data_quality`);
            # not this validator's concern.
            continue
        flexible_parsed[field_name] = flexible_dt

        strict_dt = parse_datetime_strict(raw_str)
        if strict_dt is None:
            flags.append(
                f"{field_name} value {raw_str!r} only parses under fuzzy date "
                "matching and looks implausible (e.g. free text that happens to "
                "contain numbers/a year) — verify manually"
            )
        elif strict_dt != flexible_dt:
            # Both parsers accept the string but disagree on the actual date —
            # almost always an ambiguous numeric date (e.g. "04/07/2026" reads
            # as 7 Apr under the flexible parser's MM/DD default vs 4 Jul under
            # the strict DD/MM whitelist used for Indian-context dates). This is
            # worse than an outright parse failure: both look confident, only
            # one is right.
            flags.append(
                f"{field_name} value {raw_str!r} is ambiguous: parses to "
                f"{flexible_dt.date()} in flexible mode but {strict_dt.date()} "
                "under the explicit DD/MM date whitelist — verify manually"
            )

        if flexible_dt.date() < now_anchor.date():
            flags.append(
                f"{field_name} value {raw_str!r} is dated before the email "
                f"arrived ({now_anchor.date()}) — possibly a stale or "
                "misread date"
            )

    if "deadline" in flexible_parsed and "interview_date" in flexible_parsed:
        if flexible_parsed["deadline"] > flexible_parsed["interview_date"]:
            flags.append(
                "deadline is after interview_date (illogical ordering) — "
                "verify both dates"
            )

    cgpa_raw = opp_data.get("cgpa_requirement")
    if cgpa_raw:
        match = _CGPA_RE.search(str(cgpa_raw))
        if match:
            cgpa_value = float(match.group(1))
            if not (0.0 <= cgpa_value <= 10.0):
                flags.append(
                    f"cgpa_requirement {cgpa_value} is outside the plausible "
                    "range [0, 10]"
                )

    confidence = opp_data.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        if confidence < LOW_CONFIDENCE_THRESHOLD:
            flags.append(f"low-confidence extraction (self-reported {confidence:.2f})")

    return flags
