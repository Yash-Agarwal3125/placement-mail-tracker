"""Time-related utility functions."""

import logging
from datetime import datetime, timezone

try:
    from dateutil.parser import parse as date_parse
except ImportError:
    date_parse = None

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.
    
    Example
    -------
    >>> utc_now_iso()
    '2026-05-24T12:34:56.789123+00:00'
    """
    return datetime.now(timezone.utc).isoformat()

def parse_datetime_flexible(date_str: str) -> datetime | None:
    """Parse a flexible date string into a datetime object."""
    if not date_str or not isinstance(date_str, str):
        return None
        
    try:
        # If it's an ISO format, Python can usually handle it directly 
        # but dateutil is safer for general strings.
        if date_parse:
            dt = date_parse(date_str, fuzzy=True)
            # Remove timezone for simpler arithmetic if needed, or keep it aware.
            # Let's make it naive local time for comparisons.
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
            
        # Fallback if dateutil is not installed (though it should be for robust projects)
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception as e:
        logger.debug("Could not parse datetime string '%s': %s", date_str, e)
        return None
