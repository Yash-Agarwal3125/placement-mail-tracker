"""Tests for scripts/backfill_confirmations.py (docs/design/10-confirmation-
and-reminders.md, enforce-mode flip checklist step 5).

All subject/body fixtures are SYNTHETIC.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_confirmations  # noqa: E402


def _write_fixture(corpus_dir: Path, message_id: str, subject: str, body: str) -> None:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / f"{message_id}.json").write_text(
        json.dumps({
            "message_id": message_id,
            "subject": subject,
            "sender": "noreply.cdcinfo@vitstudent.ac.in",
            "body_text": body,
            "received_at": None,
            "tier": "CONFIRMED",
            "pattern_family": "you_have_applied",
            "synthetic": True,
        }),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolate_confirmation_digest_file(tmp_path, monkeypatch):
    from placement_mail_tracker.scheduler import confirmation_digest_store

    monkeypatch.setattr(
        confirmation_digest_store, "_FLAGS_FILE", tmp_path / "confirmation_digest.json"
    )


@pytest.fixture()
def corpus_dir(tmp_path, monkeypatch):
    directory = tmp_path / "confirmations"
    monkeypatch.setattr(backfill_confirmations, "CORPUS_DIR", directory)
    return directory


def test_backfill_marks_confident_match_applied(db_manager, sample_opportunity, corpus_dir):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Cisco", "SDE"), source_email_id="seed_cisco",
    )
    drive_id = db_manager.fetch_opportunity_by_id(opp_id)["drive_id"]
    _write_fixture(
        corpus_dir, "conf_1", "Application Confirmation",
        "You have successfully applied for Cisco SDE role.",
    )

    stats = backfill_confirmations.backfill(db_manager)

    assert stats == {
        "fixtures": 1, "unknown_tier": 0, "no_confident_match": 0,
        "applied": 1, "already_applied": 0,
    }
    row = db_manager.fetch_opportunity_by_id(opp_id)
    assert row["my_status"] == "APPLIED"
    assert row["drive_id"] == drive_id


def test_backfill_is_idempotent(db_manager, sample_opportunity, corpus_dir):
    db_manager.insert_or_update_opportunity(
        sample_opportunity("Cisco", "SDE"), source_email_id="seed_cisco",
    )
    _write_fixture(
        corpus_dir, "conf_1", "Application Confirmation",
        "You have successfully applied for Cisco SDE role.",
    )

    first = backfill_confirmations.backfill(db_manager)
    second = backfill_confirmations.backfill(db_manager)

    assert first["applied"] == 1
    assert second["applied"] == 0
    assert second["already_applied"] == 1


def test_backfill_never_downgrades_shortlisted(db_manager, sample_opportunity, corpus_dir):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Cisco", "SDE"), source_email_id="seed_cisco",
    )
    drive_id = db_manager.fetch_opportunity_by_id(opp_id)["drive_id"]
    db_manager.set_my_status(drive_id, "SHORTLISTED", source="sheet")
    _write_fixture(
        corpus_dir, "conf_1", "Application Confirmation",
        "You have successfully applied for Cisco SDE role.",
    )

    stats = backfill_confirmations.backfill(db_manager)

    assert stats["already_applied"] == 1
    assert stats["applied"] == 0
    row = db_manager.fetch_opportunity_by_id(opp_id)
    assert row["my_status"] == "SHORTLISTED"


def test_backfill_skips_unknown_tier(db_manager, sample_opportunity, corpus_dir):
    db_manager.insert_or_update_opportunity(
        sample_opportunity("Cisco", "SDE"), source_email_id="seed_cisco",
    )
    _write_fixture(corpus_dir, "conf_1", "Update", "Some unrelated phrasing about Cisco.")

    stats = backfill_confirmations.backfill(db_manager)

    assert stats["unknown_tier"] == 1
    assert stats["applied"] == 0


def test_backfill_skips_no_confident_match(db_manager, corpus_dir):
    _write_fixture(
        corpus_dir, "conf_1", "Application Confirmation",
        "You have successfully applied for an unknown company.",
    )

    stats = backfill_confirmations.backfill(db_manager)

    assert stats["no_confident_match"] == 1
    assert stats["applied"] == 0


def test_backfill_dry_run_writes_nothing(db_manager, sample_opportunity, corpus_dir):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Cisco", "SDE"), source_email_id="seed_cisco",
    )
    _write_fixture(
        corpus_dir, "conf_1", "Application Confirmation",
        "You have successfully applied for Cisco SDE role.",
    )

    stats = backfill_confirmations.backfill(db_manager, dry_run=True)

    assert stats["fixtures"] == 1
    assert stats["applied"] == 0
    assert stats["already_applied"] == 0
    row = db_manager.fetch_opportunity_by_id(opp_id)
    assert row["my_status"] == "NOT_APPLIED"
