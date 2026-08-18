"""Duplicate-drive detection (safety-nets plan, Phase 3).

Phase 1 of the calendar-drift remediation plan stops *most* new duplicate
drives at insert time (``db/manager.py::insert_or_update_opportunity``), but
not all -- the EXPIRED-resolver gap found and fixed on 2026-08-18 proved a
gap can reopen without anyone noticing for months. This module is a pure,
read-only detector run at the end of every cycle: if two or more active
opportunities share a normalised company + year, that is surfaced as a
warning (log line + digest line). Detection only -- this module never
merges anything. Merging stays a reviewed, explicit operation via
``scripts/backfill_drive_identity_merge.py``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from placement_mail_tracker.extraction.rule_engine import normalize_company_name
from placement_mail_tracker.utils.time import parse_datetime_flexible

# Known-legitimate concurrent-drive counts, per
# scripts/backfill_drive_identity_merge.py's dry-run review (Honeywell
# genuinely runs 3 concurrent drives in a season -- Super Dream Internship,
# the Campus Connect Hackathon, and Aerospace; Flipkart runs 2 -- Super Dream
# PPO and Super Dream Internship). Count-based, not a blanket suppression:
# a company that fragments *beyond* its known-legitimate count still warns
# (Honeywell going from 3 rows to 5 must not stay silent forever).
KNOWN_CONCURRENT_DRIVE_COUNTS: dict[str, int] = {
    "honeywell": 3,
    "flipkart": 2,
}

# Junk/placeholder company values that must never be grouped -- otherwise
# every "Unknown Company" row (ten-plus in production) or every row a bad
# extraction routed through the same rejected-token path would collapse into
# one giant false-positive duplicate group, exactly the failure mode the
# allowlist exists to avoid for real companies.
_JUNK_COMPANIES = frozenset({"", "unknown", "unknown company"})


def _row_year(opp: dict[str, Any]) -> str:
    """Best-effort year for one opportunity row.

    Mirrors the private cross-module helper convention already used by
    ``scripts/backfill_orphaned_confirmations.py`` (``_row_year``) and
    ``db/manager.py``'s ``_get_year_from_opportunity`` rather than importing
    either -- both are private to their modules.
    """
    for value in (opp.get("email_received_at"), opp.get("created_at")):
        if not value:
            continue
        parsed = parse_datetime_flexible(str(value))
        if parsed is not None:
            return str(parsed.year)
    return str(datetime.now().year)


def find_duplicate_drive_groups(
    opportunities: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group active drives sharing normalised company + year; return only
    groups exceeding their allowed size (1, or the allowlisted count).

    Junk/unidentifiable company names are excluded before grouping (see
    ``_JUNK_COMPANIES``) so they can never form a false-positive group.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for opp in opportunities:
        norm = normalize_company_name(opp.get("company_name"))
        if not norm or norm.strip().casefold() in _JUNK_COMPANIES:
            continue
        year = _row_year(opp)
        groups[(norm, year)].append(opp)

    flagged: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, rows in groups.items():
        norm_company, _year = key
        allowed = KNOWN_CONCURRENT_DRIVE_COUNTS.get(norm_company.strip().casefold(), 1)
        if len(rows) > allowed:
            flagged[key] = rows
    return flagged


def format_duplicate_drive_warnings(
    groups: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[str]:
    """One human-readable line per flagged group, naming the drive/opportunity
    ids involved (both -- ``drive_id`` for a human, ``id`` for a query)."""
    lines: list[str] = []
    for (norm_company, year), rows in sorted(groups.items()):
        opp_ids = sorted(r.get("id") for r in rows if r.get("id") is not None)
        companies = sorted({r.get("company_name") for r in rows if r.get("company_name")})
        display_name = companies[0] if companies else norm_company
        lines.append(
            f"{display_name} ({year}): {len(rows)} active drives share this "
            f"company+year -- opportunity ids {opp_ids}"
        )
    return lines
