"""Time-related utility functions."""

import logging
import re
from datetime import datetime, timezone

try:
    from dateutil.parser import parse as date_parse
except ImportError:
    date_parse = None

logger = logging.getLogger(__name__)

# Placement emails should never reference dates older than this.
_MIN_YEAR = 2020
# Reasonable upper bound: current year + this delta.
_MAX_YEAR_DELTA = 4

# Common date formats found in placement emails, tried in order when dateutil
# is not available.  ISO variants are handled by fromisoformat() before this list.
_STRPTIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%d %B %Y %I:%M %p",   # "17 June 2026 05:30 PM"
    "%d %B %Y",             # "17 June 2026"
    "%B %d, %Y",            # "June 17, 2026"
    "%d-%b-%Y %I:%M %p",   # "17-Jun-2026 05:30 PM"
    "%d-%b-%Y",             # "17-Jun-2026"
    "%d %b %Y",             # "17 Jun 2026"
    "%B %Y",                # "June 2026"  — defaults to day 1
)

# Explicit whitelist of complete-string date formats for parse_datetime_strict.
# Unlike _STRPTIME_FORMATS (a fallback used only when dateutil is missing),
# this list is consulted unconditionally by the strict parser and is the
# entire acceptance surface — no dateutil call, fuzzy or otherwise, backs it.
# Indian-context DD/MM numeric dates ("04/07/2026", "04-07-2026") are included
# explicitly (day-month-year); MM/DD is deliberately not accepted here since
# this project's mail source is Indian placement-cell correspondence (see
# CLAUDE.md / the Gemini prompt's DMY convention note) and accepting both
# would silently reintroduce the DD/MM vs MM/DD ambiguity this parser exists
# to avoid.
_STRICT_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%d %B %Y %I:%M %p",   # "17 June 2026 05:30 PM"
    "%d %B %Y",             # "17 June 2026"
    "%B %d, %Y",            # "June 17, 2026"
    "%d-%b-%Y %I:%M %p",   # "17-Jun-2026 05:30 PM"
    "%d-%b-%Y",             # "17-Jun-2026"
    "%d %b %Y",             # "17 Jun 2026"
    "%d/%m/%Y %I:%M %p",   # "04/07/2026 03:00 PM" (DD/MM/YYYY)
    "%d/%m/%Y",             # "04/07/2026" (DD/MM/YYYY)
    "%d-%m-%Y %I:%M %p",   # "04-07-2026 03:00 PM" (DD-MM-YYYY)
    "%d-%m-%Y",             # "04-07-2026" (DD-MM-YYYY)
    "%B %Y",                # "June 2026"  — defaults to day 1
)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Example
    -------
    >>> utc_now_iso()
    '2026-05-24T12:34:56.789123+00:00'
    """
    return datetime.now(timezone.utc).isoformat()


def _parse_without_dateutil(date_str: str) -> datetime | None:
    """Try fromisoformat then the format list; return None if nothing matches."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    stripped = date_str.strip()
    for fmt in _STRPTIME_FORMATS:
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue
    return None


def human_relative_time(dt: datetime | None) -> str:
    """Return a human-readable relative time string.

    Examples: "Today, 7:43 AM" | "Yesterday, 6:18 PM" | "3 days ago" | "02 Jul 2026"
    """
    if dt is None:
        return ""
    now = datetime.now()
    days_ago = (now.date() - dt.date()).days
    hour = dt.strftime("%I").lstrip("0") or "12"
    time_str = f"{hour}:{dt.strftime('%M %p')}"
    if days_ago == 0:
        return f"Today, {time_str}"
    if days_ago == 1:
        return f"Yesterday, {time_str}"
    if 1 < days_ago < 7:
        return f"{days_ago} days ago"
    if 7 <= days_ago < 14:
        return "1 week ago"
    if 14 <= days_ago < 30:
        return f"{days_ago // 7} weeks ago"
    return f"{dt.day} {dt.strftime('%b %Y')}"


def parse_datetime_flexible(date_str: str) -> datetime | None:
    """Parse a flexible date string into a datetime object.

    Returns None (and logs a warning) for:
    - bare year strings like "2026" (FS T1.5: reject year-only)
    - dates outside the expected placement season range (FS T1.5: validate range)
    """
    if not date_str or not isinstance(date_str, str):
        return None

    # Reject bare year strings — dateutil returns Jan 1 <year> for "2026".
    if re.fullmatch(r"\d{4}", date_str.strip()):
        logger.warning("Rejected year-only date string %r", date_str)
        return None

    try:
        if date_parse:
            dt = date_parse(date_str, fuzzy=True)
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
        else:
            dt = _parse_without_dateutil(date_str)
    except Exception as e:
        logger.debug("Could not parse datetime string '%s': %s", date_str, e)
        return None

    if dt is None:
        logger.debug("No format matched for datetime string '%s'", date_str)
        return None

    # Validate against the expected placement season range.
    max_year = datetime.now().year + _MAX_YEAR_DELTA
    if dt.year < _MIN_YEAR or dt.year > max_year:
        logger.warning(
            "Date %r parsed to %s which is outside the expected range [%d, %d]",
            date_str, dt.date(), _MIN_YEAR, max_year,
        )
        return None

    return dt


def parse_datetime_strict(date_str: str) -> datetime | None:
    """Strictly parse a date string against an explicit format whitelist.

    This is a **new, additional** parser — it does not replace
    ``parse_datetime_flexible`` anywhere, per CLAUDE.md's convention that all
    stored dates (sheets, alerts, digest, action_required) are parsed with the
    flexible parser. It exists solely for the post-extraction validation layer
    (``extraction/validation.py``) to catch "plausible-looking garbage" that
    fuzzy parsing accepts as a real date — e.g. ``parse_datetime_flexible``
    resolves "Contact HR at extension 2026 in June" to a real date (fuzzy
    mode extracts "2026" and "June" and fills in the rest from today's date),
    while this function correctly rejects it because the full string never
    matches any whitelisted format.

    Deliberately does not call ``dateutil.parser.parse`` at all (not even with
    ``fuzzy=False``): matching the *entire* string against a fixed set of
    ``strptime`` formats is fully deterministic and does not depend on
    dateutil's own non-fuzzy leniency (which still tolerates some partial/
    reordered input and could drift across dateutil versions). See
    ``docs/design/03-adr-calendar-sync.md`` Decision 3 for the precedent this
    mirrors (a dedicated strict parser for one boundary, not a change to the
    shared flexible parser).

    Returns ``None`` for anything that isn't an exact, complete match to one
    of the whitelisted formats, including bare years and out-of-range years
    (same range guard as ``parse_datetime_flexible``).
    """
    if not date_str or not isinstance(date_str, str):
        return None

    stripped = date_str.strip()
    if re.fullmatch(r"\d{4}", stripped):
        return None

    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
    except ValueError:
        for fmt in _STRICT_DATE_FORMATS:
            try:
                dt = datetime.strptime(stripped, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        return None

    max_year = datetime.now().year + _MAX_YEAR_DELTA
    if dt.year < _MIN_YEAR or dt.year > max_year:
        return None

    return dt


def parse_event_datetime(date_str: str) -> datetime | None:
    """Strict variant for the calendar boundary (ADR D3): thin alias of
    parse_datetime_strict, which was built in a prior session specifically as
    the precedent for this boundary (see its own docstring)."""
    return parse_datetime_strict(date_str)
