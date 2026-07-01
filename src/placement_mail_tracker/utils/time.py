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
