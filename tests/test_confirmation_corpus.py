"""Tests for scheduler/confirmation_corpus.py: auto-capture of
APPLICATION_CONFIRMATION mails as eval fixtures (Feature 1, docs/design/10)."""

from __future__ import annotations

import json

import pytest

from placement_mail_tracker.scheduler import confirmation_corpus


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(confirmation_corpus, "CORPUS_DIR", tmp_path / "confirmations")
    monkeypatch.setattr(confirmation_corpus, "LABELS_FILE", tmp_path / "labels.csv")


def test_capture_writes_fixture_and_label_row():
    confirmation_corpus.capture_confirmation_fixture(
        message_id="msg_1",
        subject="Application Confirmation",
        sender="noreply.cdcinfo@vitstudent.ac.in",
        body="You have successfully applied.",
        received_at="2026-07-10T10:00:00+05:30",
        tier="CONFIRMED",
        pattern_family="successfully_applied_or_registered",
    )

    fixture_path = confirmation_corpus.CORPUS_DIR / "msg_1.json"
    assert fixture_path.exists()
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["tier"] == "CONFIRMED"
    assert fixture["synthetic"] is False

    labels_text = confirmation_corpus.LABELS_FILE.read_text(encoding="utf-8")
    assert "classification" in labels_text
    assert "msg_1" in labels_text


def test_capture_strips_tracking_pixels():
    body = 'Thanks. <img src="http://track.example/x" width="1" height="1"> end.'
    confirmation_corpus.capture_confirmation_fixture(
        message_id="msg_2", subject="s", sender="x", body=body,
        received_at=None, tier="UNKNOWN", pattern_family=None,
    )
    fixture = json.loads((confirmation_corpus.CORPUS_DIR / "msg_2.json").read_text())
    assert "track.example" not in fixture["body_text"]


def test_capture_never_raises_on_write_failure(monkeypatch):
    monkeypatch.setattr(
        confirmation_corpus, "CORPUS_DIR", confirmation_corpus.CORPUS_DIR / "\0bad"
    )
    confirmation_corpus.capture_confirmation_fixture(
        message_id="msg_3", subject="s", sender="x", body="b",
        received_at=None, tier="UNKNOWN", pattern_family=None,
    )
