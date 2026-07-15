"""Auto-capture of APPLICATION_CONFIRMATION mails as eval fixtures.

Feature 1 (docs/design/10-confirmation-and-reminders.md): zero real CDC
confirmation samples exist anywhere (docs/design/08-confirmation-audit.md
blocker 1), so the system builds its own ground-truth corpus once real mail
starts arriving, every classified confirmation mail regardless of mode/tier.

These are the user's own mails about their own applications, not another
student's data, so "sanitized" here is limited to stripping tracking-pixel
noise from the HTML body (matching the shape of the existing corpus fixtures)
rather than a full redaction pass.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CORPUS_DIR = Path("scripts/eval/corpus/confirmations")
LABELS_FILE = Path("scripts/eval/labels.csv")

_LABELS_HEADER = [
    "message_id", "received", "subject", "field",
    "rule_value", "prefill_label", "corrected_label", "notes",
]

_TRACKING_PIXEL_RE = re.compile(r'<img[^>]*width="1"[^>]*>', re.IGNORECASE)


def _sanitize_body(body: str) -> str:
    return _TRACKING_PIXEL_RE.sub("", body or "")


def capture_confirmation_fixture(
    *,
    message_id: str,
    subject: str,
    sender: str,
    body: str,
    received_at: str | None,
    tier: str,
    pattern_family: str | None,
) -> None:
    """Write a sanitized fixture + a blank-for-human-review labels.csv row.

    Never raises — a corpus-capture failure must not break the pipeline.
    """
    try:
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
        fixture = {
            "message_id": message_id,
            "subject": subject,
            "sender": sender,
            "received_at": received_at,
            "body_text": _sanitize_body(body),
            "tier": tier,
            "pattern_family": pattern_family,
            "synthetic": False,
        }
        (CORPUS_DIR / f"{message_id}.json").write_text(
            json.dumps(fixture, indent=2), encoding="utf-8"
        )
        _append_classification_label(message_id, subject, received_at, tier)
    except Exception as error:
        logger.warning("Could not capture confirmation fixture: %s", error)


def _append_classification_label(
    message_id: str, subject: str, received_at: str | None, tier: str
) -> None:
    """Add a `field=classification` row for this fixture only (docs/design/08
    C5 decision: no scoring/floor is wired up for this field — a human fills
    `corrected_label` once a real sample lands; see the doc's off-season note
    about not retrofitting classification scoring for the other 7 types).
    """
    is_new = not LABELS_FILE.exists()
    LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LABELS_FILE.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(_LABELS_HEADER)
        writer.writerow([
            message_id, received_at or "", subject, "classification",
            "", tier, "", "auto-captured confirmation mail; human fills corrected_label",
        ])
