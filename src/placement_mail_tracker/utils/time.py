"""Time-related utility functions."""

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()
