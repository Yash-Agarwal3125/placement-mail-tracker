"""Time-related utility functions."""

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.
    
    Example
    -------
    >>> utc_now_iso()
    '2026-05-24T12:34:56.789123+00:00'
    """
    return datetime.now(timezone.utc).isoformat()
