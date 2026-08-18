"""Duplicate-drive warning lines, persisted between runs until the next
daily digest.

Safety-nets plan Phase 3: the end-of-run duplicate-drive detector
(``utils/duplicate_drive_detection.py``) runs every cycle, but the digest
only sends once a day -- same small-JSON-file pattern as
``calendar_flags_store.py`` / ``confirmation_digest_store.py`` so these
lines survive between 3-hourly runs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FLAGS_FILE = Path("data/duplicate_drive_flags.json")


def append_duplicate_drive_lines(lines: list[str]) -> None:
    """Append new warning lines, deduping against what's already pending."""
    if not lines:
        return
    existing = _read()
    merged = existing + [line for line in lines if line not in existing]
    _write(merged)


def pop_pending_duplicate_drive_lines() -> list[str]:
    """Return pending warning lines and clear them (consumed by the digest)."""
    lines = _read()
    if lines:
        _write([])
    return lines


def _read() -> list[str]:
    try:
        if _FLAGS_FILE.exists():
            return json.loads(_FLAGS_FILE.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning("Could not read duplicate drive flags state: %s", error)
    return []


def _write(lines: list[str]) -> None:
    try:
        _FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FLAGS_FILE.write_text(json.dumps(lines, indent=2), encoding="utf-8")
    except Exception as error:
        logger.warning("Could not persist duplicate drive flags state: %s", error)
