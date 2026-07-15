"""Backfill for confirmation mails that arrived while CONFIRMATION_MODE=observe.

docs/design/10-confirmation-and-reminders.md, "Enforce-mode flip checklist"
step 5: after flipping to enforce, optionally re-run detection/matching
against every captured confirmation fixture
(scripts/eval/corpus/confirmations/*.json, written by
scheduler/confirmation_corpus.py for every classified confirmation mail
regardless of mode) and apply the same source="automation" ladder write that
live enforce-mode processing would have applied at the time.

Uses current code (not the tier stored in the fixture at capture time) so a
later pattern-family fix also benefits mail captured before that fix landed.
Never touches current_status or creates a drive (same D3 guarantee as the
live path in scheduler/runner.py::_handle_confirmation_mail) -- goes through
DatabaseManager.set_my_status(source="automation"), which is upgrade-only,
so re-running this script is a no-op for anything already applied.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.confirmation import (
    detect_confirmation_tier,
    extract_reference_id,
    find_confident_drive_match,
)
from placement_mail_tracker.scheduler.confirmation_digest_store import append_confirmation_lines
from placement_mail_tracker.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)

CORPUS_DIR = Path("scripts/eval/corpus/confirmations")


def backfill(database: DatabaseManager, *, dry_run: bool = False) -> dict[str, int]:
    """Apply ladder writes for every captured confirmation fixture.

    Returns per-outcome counts. Never raises for a single bad fixture file --
    logs a warning and continues, same fail-soft posture as the live path.
    """
    stats = {
        "fixtures": 0,
        "unknown_tier": 0,
        "no_confident_match": 0,
        "applied": 0,
        "already_applied": 0,
    }
    if not CORPUS_DIR.exists():
        return stats

    active_opportunities = database.get_active_opportunities()
    digest_lines: list[str] = []

    for path in sorted(CORPUS_DIR.glob("*.json")):
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - one bad fixture must not abort the backfill
            logger.warning("Could not read confirmation fixture %s: %s", path, error)
            continue

        stats["fixtures"] += 1
        subject = fixture.get("subject") or ""
        body = fixture.get("body_text") or ""

        tier, _family = detect_confirmation_tier(subject, body)
        if tier == "UNKNOWN":
            # Escape valve (D1/feature_1_spec): never writes, even in enforce
            # mode -- backfill must not be a way to bypass that guarantee.
            stats["unknown_tier"] += 1
            continue

        reference_id = extract_reference_id(subject, body)
        match, _candidates = find_confident_drive_match(
            subject, body, active_opportunities, reference_id=reference_id
        )
        if match is None:
            stats["no_confident_match"] += 1
            continue

        company = match.opportunity.get("company_name")
        drive_id = match.opportunity.get("drive_id")

        if dry_run:
            logger.info("PLAN: would mark %s APPLIED (backfill, %s)", company, path.name)
            continue

        changed = database.set_my_status(drive_id, "APPLIED", source="automation")
        if changed:
            stats["applied"] += 1
            digest_lines.append(f"backfill: marked {company} APPLIED (confirmation mail).")
        else:
            stats["already_applied"] += 1

    if digest_lines:
        append_confirmation_lines(digest_lines)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill APPLIED status for confirmation mails captured while "
            "CONFIRMATION_MODE=observe (docs/design/10-confirmation-and-"
            "reminders.md, enforce-mode flip checklist step 5)."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to the database.",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(
        settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    database = DatabaseManager(settings.database_path)

    stats = backfill(database, dry_run=args.dry_run)
    print(
        f"Fixtures scanned: {stats['fixtures']} | unknown tier: {stats['unknown_tier']} | "
        f"no confident match: {stats['no_confident_match']} | applied: {stats['applied']} | "
        f"already applied (no-op): {stats['already_applied']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
