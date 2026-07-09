"""Calendar sync anomaly lines, persisted between runs until the next digest.

docs/design/04-integration-spec.md §4 point 8: the calendar step's
``CalendarSyncResult.flagged`` lines (null-date drives, dropped collisions,
unparseable dates) need to survive between 3-hourly runs until the next daily
digest, without a schema change. A small JSON file under ``data/`` is the
simplest fit — same pattern as ``data/oauth_alert_state.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FLAGS_FILE = Path("data/calendar_flags.json")


def append_calendar_flags(lines: list[str]) -> None:
    """Append new anomaly lines, deduping against what's already pending."""
    if not lines:
        return
    existing = _read()
    merged = existing + [line for line in lines if line not in existing]
    _write(merged)


def pop_pending_calendar_flags() -> list[str]:
    """Return pending anomaly lines and clear them (consumed by the digest)."""
    lines = _read()
    if lines:
        _write([])
    return lines


def _read() -> list[str]:
    try:
        if _FLAGS_FILE.exists():
            return json.loads(_FLAGS_FILE.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning("Could not read calendar flags state: %s", error)
    return []


def _write(lines: list[str]) -> None:
    try:
        _FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FLAGS_FILE.write_text(json.dumps(lines, indent=2), encoding="utf-8")
    except Exception as error:
        logger.warning("Could not persist calendar flags state: %s", error)
