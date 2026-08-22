"""Safety-nets plan (2026-08-18), Phase 1: unmatched_confirmations needs a
reader and a way to be emptied.

Covers the migration idiom (resolved/resolved_at columns on an
already-existing table), the ``unresolved_only`` filter, and auto-resolution
on a later successful attach. (The digest-section formatting these fed used
to be tested here too, before the digest feature was removed.)
"""

from __future__ import annotations

import sqlite3

from placement_mail_tracker.db.manager import DatabaseManager


class TestMigrationAddsResolvedColumns:
    def test_legacy_table_without_resolved_columns_gets_migrated(self):
        """Simulate a pre-existing DB file created before this phase shipped:
        an unmatched_confirmations table with no resolved/resolved_at
        columns. DatabaseManager's normal startup migration must add them
        without erroring, following the existing ALTER TABLE idiom."""
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE unmatched_confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT NOT NULL,
                extracted_text TEXT,
                candidates TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO unmatched_confirmations "
            "(gmail_message_id, extracted_text, created_at) VALUES (?, ?, ?);",
            ("legacy-msg", "legacy row", "2026-01-01T00:00:00Z"),
        )
        connection.commit()

        db = DatabaseManager(connection=connection)

        columns = {
            row["name"]
            for row in db.connection.execute(
                "PRAGMA table_info(unmatched_confirmations);"
            ).fetchall()
        }
        assert "resolved" in columns
        assert "resolved_at" in columns

        rows = db.fetch_unmatched_confirmations()
        assert len(rows) == 1
        assert rows[0]["resolved"] == 0


class TestUnresolvedOnlyFilter:
    def test_unresolved_only_excludes_resolved_rows(self, db_manager):
        db_manager.insert_unmatched_confirmation(
            gmail_message_id="msg-1", extracted_text="Acme / Role A", candidates=[]
        )
        db_manager.insert_unmatched_confirmation(
            gmail_message_id="msg-2", extracted_text="Beta / Role B", candidates=[]
        )
        db_manager.resolve_unmatched_confirmations_for_message("msg-1")

        all_rows = db_manager.fetch_unmatched_confirmations()
        unresolved = db_manager.fetch_unmatched_confirmations(unresolved_only=True)

        assert len(all_rows) == 2
        assert len(unresolved) == 1
        assert unresolved[0]["gmail_message_id"] == "msg-2"


class TestAutoResolveOnAttach:
    def test_resolve_marks_row_resolved_with_timestamp(self, db_manager):
        db_manager.insert_unmatched_confirmation(
            gmail_message_id="msg-1", extracted_text="text", candidates=[]
        )
        changed = db_manager.resolve_unmatched_confirmations_for_message("msg-1")
        assert changed == 1

        row = db_manager.connection.execute(
            "SELECT resolved, resolved_at FROM unmatched_confirmations "
            "WHERE gmail_message_id = ?;",
            ("msg-1",),
        ).fetchone()
        assert row["resolved"] == 1
        assert row["resolved_at"]

    def test_successful_insert_auto_resolves_prior_unmatched_row(self, db_manager):
        """The scenario the phase exists for: a mail was previously parked
        in unmatched_confirmations, then a later replay of the *same*
        gmail_message_id succeeds (attaches or creates a drive) -- the old
        row must be marked resolved automatically."""
        db_manager.insert_unmatched_confirmation(
            gmail_message_id="honeywell-aero-1",
            extracted_text="Honeywell Aerospace: ambiguous",
            candidates=[],
        )

        opp_id, created = db_manager.insert_or_update_opportunity(
            {
                "company_name": "Honeywell Aerospace",
                "role": "Intern",
                "current_status": "OPEN",
                "eligibility_status": "ELIGIBLE",
            },
            source_email_id="honeywell-aero-1",
        )

        assert opp_id is not None
        assert created is True
        unresolved = db_manager.fetch_unmatched_confirmations(unresolved_only=True)
        assert unresolved == []

    def test_successful_update_also_auto_resolves(self, db_manager):
        """The update branch (existing drive found, no create) must resolve
        too, not just the insert branch."""
        opp_id, _ = db_manager.insert_or_update_opportunity(
            {
                "company_name": "Accenture",
                "role": "Intern",
                "current_status": "OPEN",
                "eligibility_status": "ELIGIBLE",
            },
            source_email_id="acc-1",
        )
        assert opp_id is not None

        db_manager.insert_unmatched_confirmation(
            gmail_message_id="acc-1", extracted_text="stray", candidates=[]
        )

        # Re-processing the same mail again updates the same drive (no
        # changes -> "duplicate_seen" branch) and must still resolve.
        db_manager.insert_or_update_opportunity(
            {
                "company_name": "Accenture",
                "role": "Intern",
                "current_status": "OPEN",
                "eligibility_status": "ELIGIBLE",
            },
            source_email_id="acc-1",
        )

        unresolved = db_manager.fetch_unmatched_confirmations(unresolved_only=True)
        assert unresolved == []

    def test_ambiguous_route_does_not_resolve_anything(self, db_manager):
        """The ambiguous branch returns (None, False) before either success
        return path -- it must never mark anything resolved (it didn't
        attach to anything)."""
        for company in ("Flipkart Super Dream PPO", "Flipkart Super Dream Internship"):
            db_manager.insert_or_update_opportunity(
                {
                    "company_name": "Flipkart",
                    "role": company,
                    "current_status": "OPEN",
                    "eligibility_status": "ELIGIBLE",
                },
                source_email_id=f"seed-{company}",
            )

        db_manager.insert_unmatched_confirmation(
            gmail_message_id="flipkart-ambiguous", extracted_text="prior", candidates=[]
        )

        opp_id, created = db_manager.insert_or_update_opportunity(
            {
                "company_name": "Flipkart",
                "role": "Online test is scheduled",
                "current_status": "OA",
            },
            source_email_id="flipkart-ambiguous",
            email_classification="OA_UPDATE",
        )

        assert opp_id is None
        assert created is False
        unresolved = db_manager.fetch_unmatched_confirmations(unresolved_only=True)
        # The pre-existing row for this message must still be unresolved --
        # AND a fresh row was written for the ambiguous attempt itself.
        assert len(unresolved) == 2


