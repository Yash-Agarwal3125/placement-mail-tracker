"""Confirmation-mail digest lines, persisted between runs until the next
daily digest.

Feature 1 (docs/design/10-confirmation-and-reminders.md): every
APPLICATION_CONFIRMATION mail processed this run — tier, match result, and
the action taken (or would-have-taken in observe mode) — needs a visible
trace in the digest. Same small-JSON-file pattern as calendar_flags_store.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FLAGS_FILE = Path("data/confirmation_digest.json")


def append_confirmation_lines(lines: list[str]) -> None:
    if not lines:
        return
    existing = _read()
    merged = existing + [line for line in lines if line not in existing]
    _write(merged)


def pop_pending_confirmation_lines() -> list[str]:
    lines = _read()
    if lines:
        _write([])
    return lines


def _read() -> list[str]:
    try:
        if _FLAGS_FILE.exists():
            return json.loads(_FLAGS_FILE.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning("Could not read confirmation digest state: %s", error)
    return []


def _write(lines: list[str]) -> None:
    try:
        _FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FLAGS_FILE.write_text(json.dumps(lines, indent=2), encoding="utf-8")
    except Exception as error:
        logger.warning("Could not persist confirmation digest state: %s", error)
