"""Tests for utils/trusted_senders.py and dynamic trusted sender filtering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from placement_mail_tracker.gmail.filters import calculate_relevance_score, is_placement_mail
from placement_mail_tracker.utils.trusted_senders import TrustedSender, TrustedSenderManager


@pytest.fixture()
def temp_storage_path(tmp_path) -> Path:
    return tmp_path / "trusted_senders.json"


@pytest.fixture()
def manager(temp_storage_path) -> TrustedSenderManager:
    return TrustedSenderManager(storage_path=temp_storage_path)


# ---------------------------------------------------------------------------
# TrustedSenderManager Unit Tests
# ---------------------------------------------------------------------------

class TestTrustedSenderManager:
    def test_parse_from_header_handles_full_address(self, manager):
        email, name = manager.parse_from_header("Career Development Centre <cdc@college.edu>")
        assert email == "cdc@college.edu"
        assert name == "Career Development Centre"

    def test_parse_from_header_handles_simple_email(self, manager):
        email, name = manager.parse_from_header("cdc@college.edu")
        assert email == "cdc@college.edu"
        assert name == ""

    def test_parse_from_header_handles_mixed_case(self, manager):
        email, name = manager.parse_from_header("  CDC-Office <CDC.Office@College.edu> ")
        assert email == "cdc.office@college.edu"
        assert name == "CDC-Office"

    def test_calculate_sender_score_matching_keywords(self, manager):
        score, keywords = manager.calculate_sender_score("cdc@college.edu", "Career Development Centre")
        assert score >= 50
        assert "display:career development center" in keywords or "display:career development centre" in keywords
        assert "email:cdc" in keywords

    def test_calculate_sender_score_boosted_by_subject(self, manager):
        # Even with lower display/email keywords, a strong placement subject boosts score
        score, keywords = manager.calculate_sender_score(
            "helpdesk@college.edu",
            "Helpdesk",
            subject="Interview schedule for Software Engineer opportunity",
        )
        assert score >= 35
        assert "subject:interview schedule" in keywords

    def test_process_and_discover_saves_trusted_sender(self, manager, temp_storage_path):
        is_trusted, score = manager.process_and_discover(
            "Career Placements <placement-office@college.edu>",
            subject="New Internship Drive",
        )
        assert is_trusted is True
        assert score >= 50

        # Verify persistent storage
        assert temp_storage_path.exists()
        loaded = json.loads(temp_storage_path.read_text(encoding="utf-8"))
        assert len(loaded) == 1
        assert loaded[0]["email"] == "placement-office@college.edu"
        assert loaded[0]["display_name"] == "Career Placements"
        assert loaded[0]["score"] == score

    def test_process_and_discover_ignores_spam_domains(self, manager):
        is_trusted, score = manager.process_and_discover(
            "Medium Placements <newsletter@medium.com>",
            subject="Daily Career Placements Digest",
        )
        assert is_trusted is False
        assert score == 0
        assert len(manager.senders) == 0

    def test_score_updates_over_time_increases_existing_score(self, manager):
        # 1. First encounter with medium score keywords
        manager.process_and_discover("Helpdesk <helpdesk@college.edu>", subject="General Q&A")
        initial_score = manager.senders["helpdesk@college.edu"].score
        assert initial_score < 50  # Lower than default threshold

        # 2. Second encounter with high-confidence subject boost
        is_trusted, updated_score = manager.process_and_discover(
            "Helpdesk <helpdesk@college.edu>",
            subject="URGENT: Placement Interview schedule",
        )
        assert updated_score > initial_score
        assert manager.senders["helpdesk@college.edu"].score == updated_score


# ---------------------------------------------------------------------------
# Gmail Filters Integration Tests
# ---------------------------------------------------------------------------

class TestGmailFiltersDynamicDiscovery:
    def test_email_from_newly_discovered_trusted_sender_is_auto_approved(self, monkeypatch, temp_storage_path):
        # Configure monkeypatch storage path to point to a temporary test file
        monkeypatch.setattr(
            "placement_mail_tracker.gmail.filters.TrustedSenderManager",
            lambda: TrustedSenderManager(storage_path=temp_storage_path),
        )

        # Send email from a highly trusted institutional sender name
        decision = is_placement_mail(
            sender="CDC Placements Office <cdc@vit.ac.in>",
            subject="Hiring Update",
            body="Applications are open.",
        )

        assert decision.is_placement is True
        assert "trusted_sender:80" in decision.matched_sender_terms or any("trusted_sender" in term for term in decision.matched_sender_terms)
        
        # Verify the sender is written to the persistent JSON
        assert temp_storage_path.exists()
        loaded = json.loads(temp_storage_path.read_text(encoding="utf-8"))
        assert loaded[0]["email"] == "cdc@vit.ac.in"
