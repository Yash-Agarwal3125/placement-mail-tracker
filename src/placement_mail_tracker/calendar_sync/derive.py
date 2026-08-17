"""Drive rows -> desired calendar events (docs/design/04-integration-spec.md §3.2).

Pure module: no I/O, no Google API calls. ``CalendarEvent`` is the shared
interface fixed in Step 1; ``derive_events`` is implemented against it by the
"calendar-derive" subagent.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.extraction.rule_engine import normalize_company_name
from placement_mail_tracker.utils.time import parse_event_datetime


class CalendarEvent(BaseModel):
    """One desired Google Calendar event derived from an opportunities row."""

    opportunity_id: int
    drive_id: str | None
    event_type: Literal["DEADLINE", "OA", "INTERVIEW"]
    title: str
    start_iso: str
    end_iso: str
    all_day: bool
    location: str | None
    description: str
    reminder_minutes: list[int]

    def content_hash(self) -> str:
        """sha256(start_iso|end_iso|title|location) — the diff key (ADR D2)."""
        parts = "|".join(
            [self.start_iso, self.end_iso, self.title, self.location or ""]
        )
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()


# Placeholder company values that mean "extraction failed", not a real drive.
# Mirrors scheduler/alert_generator.py:15 / scheduler/runner.py:80 (private to
# those modules, replicated here rather than imported).
_UNIDENTIFIED_COMPANIES = frozenset({"", "unknown", "unknown company"})

# A raw date string is "timed" (has a time-of-day component) when it contains
# a ':' (24h/12h clock), an am/pm marker, or the word "hrs". Anything else is
# treated as a date-only, all-day event (ADR D3 / B7).
_TIME_TOKEN_RE = re.compile(r":|am|pm|hrs", re.IGNORECASE)

_EVENT_LABELS: dict[str, str] = {
    "DEADLINE": "Apply by deadline",
    "OA": "OA",
    "INTERVIEW": "Interview",
}

# Cause 2 / Phase 3 (calendar-drift remediation, "Round verdicts don't
# cascade forward"): selection is a ladder -- you cannot reach round n+1 of
# a drive you were cut from at round n. OFFER is included as a possible
# *source* round (e.g. a manually-asserted verdict) even though it is not a
# CalendarEvent.event_type on its own; is_round_excluded tolerates being
# asked about a round outside this tuple (DEADLINE) by returning False.
ROUND_ORDER: tuple[str, ...] = ("OA", "INTERVIEW", "OFFER")


def is_round_excluded(
    roster_verdicts: dict[tuple[int, str], dict[str, Any]],
    opportunity_id: int | None,
    round_name: str,
) -> bool:
    """Single shared resolver for "is this drive/round excluded?".

    Used by both ``derive_events`` and ``calendar_sync/sync.py``'s
    ``_null_date_pass`` so the two never disagree -- disagreement is what
    makes an event flicker between created and deleted on alternate runs.

    Contract:
    - A direct ``MATCHED`` verdict at ``round_name`` itself always wins
      (revocable exclusion -- the "shortlisted after all" correction path).
    - A direct ``NOT_MATCHED`` verdict at ``round_name`` excludes it.
    - Otherwise, a ``NOT_MATCHED`` verdict at any *earlier* round of the
      same drive (per ``ROUND_ORDER``) excludes it too (the cascade).
    - Any other case (no verdict, ``AMBIGUOUS``, ``NO_ROSTER``) does not
      exclude -- unproven exclusion must not resolve toward "hidden".
    - A ``round_name`` outside ``ROUND_ORDER`` (e.g. ``DEADLINE``) is never
      roster-gated and always returns False.
    """
    if round_name not in ROUND_ORDER or opportunity_id is None:
        return False

    direct = roster_verdicts.get((opportunity_id, round_name))
    if direct is not None:
        verdict = direct.get("verdict")
        if verdict == "MATCHED":
            return False
        if verdict == "NOT_MATCHED":
            return True

    idx = ROUND_ORDER.index(round_name)
    for earlier_round in ROUND_ORDER[:idx]:
        earlier = roster_verdicts.get((opportunity_id, earlier_round))
        if earlier is not None and earlier.get("verdict") == "NOT_MATCHED":
            return True

    return False


def _is_identifiable_company(name: str | None) -> bool:
    """Return True when ``name`` is a real company (not blank/Unknown)."""
    return bool(name) and str(name).strip().casefold() not in _UNIDENTIFIED_COMPANIES


def _has_time_token(raw: str) -> bool:
    return bool(_TIME_TOKEN_RE.search(raw))


def _gmail_link(opp: dict[str, Any]) -> str:
    """Plain-URL variant of sheets_sync.py:953 ``_gmail_link`` (no spreadsheet
    HYPERLINK() formula needed in a calendar event description)."""
    target = opp.get("source_thread_id") or opp.get("source_email_id")
    if not target:
        return ""
    return f"https://mail.google.com/mail/u/0/#inbox/{target}"


def _build_description(opp: dict[str, Any]) -> str:
    role = opp.get("role") or ""
    package = opp.get("package_or_stipend") or ""
    action_required = opp.get("action_required") or ""
    drive_id = opp.get("drive_id") or ""
    link = _gmail_link(opp)
    return (
        f"Role: {role}\n"
        f"Package/Stipend: {package}\n"
        f"Action required: {action_required}\n"
        f"Drive ID: {drive_id}\n"
        f"Gmail: {link}"
    )


def _derive_single_event(
    opp: dict[str, Any],
    event_type: Literal["DEADLINE", "OA", "INTERVIEW"],
    raw_date: Any,
    settings: Settings,
    anomalies: list[str],
) -> CalendarEvent | None:
    """Build one CalendarEvent from a single raw date column, or append an
    anomaly and return None when the date is missing/unparseable."""
    if not raw_date or not isinstance(raw_date, str):
        return None

    company_name = opp.get("company_name") or ""
    parsed = parse_event_datetime(raw_date)
    if parsed is None:
        anomalies.append(
            f"{company_name}: {event_type} date '{raw_date}' could not be "
            "parsed — no calendar event created"
        )
        return None

    all_day = not _has_time_token(raw_date)

    if all_day:
        date_str = parsed.date().isoformat()
        start_iso = date_str
        end_iso = date_str
    else:
        tz = ZoneInfo(settings.calendar_timezone)
        localized = parsed.replace(tzinfo=tz)
        if event_type == "DEADLINE":
            end_dt = localized
            start_dt = end_dt - timedelta(minutes=30)
        else:
            start_dt = localized
            end_dt = start_dt + timedelta(hours=1)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

    label = _EVENT_LABELS[event_type]
    title = f"{company_name} — {label}"
    reminder_minutes = (
        settings.calendar_deadline_reminder_minutes
        if event_type == "DEADLINE"
        else settings.calendar_event_reminder_minutes
    )

    return CalendarEvent(
        opportunity_id=opp["id"],
        drive_id=opp.get("drive_id"),
        event_type=event_type,
        title=title,
        start_iso=start_iso,
        end_iso=end_iso,
        all_day=all_day,
        location=opp.get("work_location"),
        description=_build_description(opp),
        reminder_minutes=list(reminder_minutes),
    )


def _apply_collision_guard(
    events: list[CalendarEvent],
    normalized_companies: dict[tuple[int, str], str],
) -> tuple[list[CalendarEvent], list[str]]:
    """ADR D6/B6: drop duplicate events from different opportunity_ids that
    share (normalized company_name, event_type, date); keep the lower
    opportunity_id."""
    anomalies: list[str] = []
    groups: dict[tuple[str, str, str], list[CalendarEvent]] = defaultdict(list)
    for event in events:
        norm_company = normalized_companies[(event.opportunity_id, event.event_type)]
        date_key = event.start_iso[:10]
        groups[(norm_company, event.event_type, date_key)].append(event)

    drop_keys: set[tuple[int, str]] = set()
    for (norm_company, event_type, date_key), group in groups.items():
        distinct_opp_ids = {e.opportunity_id for e in group}
        if len(distinct_opp_ids) <= 1:
            continue
        keeper = min(group, key=lambda e: e.opportunity_id)
        for dup in group:
            if dup.opportunity_id == keeper.opportunity_id:
                continue
            drop_keys.add((dup.opportunity_id, dup.event_type))
            anomalies.append(
                f"Duplicate {event_type} event for {norm_company} on {date_key} "
                f"(opportunity_id={dup.opportunity_id}) dropped — kept "
                f"opportunity_id={keeper.opportunity_id}"
            )

    kept = [e for e in events if (e.opportunity_id, e.event_type) not in drop_keys]
    return kept, anomalies


def derive_events(
    opportunities: list[dict[str, Any]],
    settings: Settings,
    roster_verdicts: dict[tuple[int, str], dict[str, Any]] | None = None,
) -> tuple[list[CalendarEvent], list[str]]:
    """Map each visible drive to 0-3 events; returns (events, anomalies).

    Implemented by the "calendar-derive" subagent per spec §3.2.

    ``roster_verdicts`` (docs/design/16 Phase D, docs/design/15 §3.3):
    keyed by (opportunity_id, event_type). A round with a stored
    ``NOT_MATCHED`` verdict is never admitted, even if it's otherwise
    eligible -- a roster explicitly said this student isn't on it for that
    specific round. Cause 2 / Phase 3: that exclusion also cascades
    forward -- a ``NOT_MATCHED`` at an earlier round of the same drive
    (``ROUND_ORDER`` = OA -> INTERVIEW -> OFFER) suppresses a later round
    too, unless that later round itself has a direct ``MATCHED`` verdict
    (see ``is_round_excluded``, shared with ``calendar_sync/sync.py``'s
    ``_null_date_pass`` so the two never disagree). Any other case (no
    verdict row -- the round was never evaluated, or ``AMBIGUOUS``/
    ``NO_ROSTER`` -- the roster didn't parse) is treated exactly as today:
    admitted by the existing eligibility/kind/mode rules. Doc 15's hard
    rule cuts both ways -- an unproven exclusion must not
    resolve toward "hidden" any more than an unproven inclusion resolves
    toward "shortlisted".
    """
    events: list[CalendarEvent] = []
    anomalies: list[str] = []
    normalized_companies: dict[tuple[int, str], str] = {}
    roster_verdicts = roster_verdicts or {}

    for opp in opportunities:
        # docs/design/16 Phase 6: only real placement drives reach the
        # calendar. A hackathon/scholarship/workshop/research posting can be
        # ELIGIBLE (nothing disqualified it) without being a drive at all —
        # gating on drive_kind here covers the DEADLINE branch too, unlike
        # calendar_sync_mode below which only ever touched OA/INTERVIEW.
        if (opp.get("drive_kind") or "PLACEMENT") != "PLACEMENT":
            continue
        eligibility_status = opp.get("eligibility_status") or ""
        if "NOT_ELIGIBLE" in eligibility_status:
            continue
        if not _is_identifiable_company(opp.get("company_name")):
            continue

        norm_company = normalize_company_name(opp.get("company_name"))

        deadline_event = _derive_single_event(
            opp, "DEADLINE", opp.get("deadline"), settings, anomalies
        )
        if deadline_event is not None:
            events.append(deadline_event)
            normalized_companies[(deadline_event.opportunity_id, "DEADLINE")] = norm_company

        include_oa_interview = settings.calendar_sync_mode == "all_eligible" or opp.get(
            "my_status"
        ) not in ("NOT_APPLIED", "", None)
        if include_oa_interview:
            for event_type, raw_date in (
                ("OA", opp.get("oa_date")),
                ("INTERVIEW", opp.get("interview_date")),
            ):
                if is_round_excluded(roster_verdicts, opp.get("id"), event_type):
                    continue
                event = _derive_single_event(opp, event_type, raw_date, settings, anomalies)
                if event is not None:
                    events.append(event)
                    normalized_companies[(event.opportunity_id, event_type)] = norm_company

    events, collision_anomalies = _apply_collision_guard(events, normalized_companies)
    anomalies.extend(collision_anomalies)

    return events, anomalies
