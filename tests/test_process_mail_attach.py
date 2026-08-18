"""Tests for Cause 1 / Phase 1 (calendar-drift remediation plan, 2026-08-17):
process mails attach to an existing drive, never create one.

``db/manager.py::insert_or_update_opportunity`` resolves a process-
classification mail (OA_UPDATE, SHORTLIST_UPDATE, INTERVIEW_UPDATE,
OFFER_UPDATE, APPLICATION_CONFIRMATION) that misses thread/hash matching
against existing active same-company-year drives before ever considering an
insert -- and attaches to *none* of them when more than one candidate
survives, routing to ``unmatched_confirmations`` instead of guessing.
"""

from __future__ import annotations

from placement_mail_tracker.db.manager import DatabaseManager


def _seed(db_manager: DatabaseManager, **overrides) -> int:
    base = {
        "company_name": "Blackrock",
        "role": "s with the respective details below",
        "internship_or_fulltime": "internship",
        "current_status": "OPEN",
        "eligibility_status": "ELIGIBLE",
    }
    base.update(overrides)
    opp_id, _ = db_manager.insert_or_update_opportunity(
        base, source_email_id=f"seed-{base['company_name']}"
    )
    return opp_id


class TestSingleActiveCandidateAttaches:
    """Blackrock/Foodhub acceptance criteria: a process mail with an
    unstable/differing role attaches to the sole active drive for that
    company+year instead of minting a new row."""

    def test_blackrock_next_round_mail_attaches_not_creates(self, db_manager):
        seeded_id = _seed(db_manager, company_name="Blackrock")
        before = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        incoming = {
            "company_name": "Blackrock",
            "role": "Software Engineering",
            "current_status": "INTERVIEW",
        }
        opp_id, created = db_manager.insert_or_update_opportunity(
            incoming,
            source_email_id="blackrock_next_round",
            source_thread_id="unrelated-thread",
            email_classification="INTERVIEW_UPDATE",
        )

        after = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        assert opp_id == seeded_id
        assert created is False
        assert after == before

    def test_foodhub_ppt_and_next_round_mail_attaches_not_creates(self, db_manager):
        seeded_id = _seed(
            db_manager, company_name="Foodhub", role="Software Engineer"
        )
        before = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        incoming = {
            "company_name": "Foodhub",
            "role": "Full Stack Developer",
            "current_status": "SHORTLISTED",
        }
        opp_id, created = db_manager.insert_or_update_opportunity(
            incoming,
            source_email_id="foodhub_ppt",
            email_classification="SHORTLIST_UPDATE",
        )

        after = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        assert opp_id == seeded_id
        assert created is False
        assert after == before

    def test_non_process_classification_still_creates_new_row(self, db_manager):
        """The attach behaviour is scoped to process classifications only --
        a NEW_DRIVE-style posting for a genuinely different role at the same
        company/year must still be free to create its own row."""
        _seed(db_manager, company_name="Nvidia", role="SDE Intern")
        before = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        incoming = {
            "company_name": "Nvidia",
            "role": "Hardware Engineer",
            "current_status": "OPEN",
        }
        _opp_id, created = db_manager.insert_or_update_opportunity(
            incoming, source_email_id="nvidia_new_posting", email_classification="NEW_DRIVE"
        )

        after = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        assert created is True
        assert after == before + 1


class TestAmbiguousCandidatesRouteToReview:
    """Flipkart acceptance criteria: two live candidates for the same
    company+year and an incompatible program name -> attach to neither."""

    def test_two_incompatible_candidates_attach_to_none(self, db_manager):
        ppo_id = _seed(db_manager, company_name="Flipkart", role="Super Dream PPO")
        intern_id = _seed(
            db_manager,
            company_name="Flipkart",
            role="Planning Intern",
            source_email_id="seed-Flipkart-2",
        )
        # Force distinct unique_hash/drive rows for both seeds (same company
        # collides on the seed's default source_email_id otherwise).
        assert ppo_id != intern_id

        before = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        incoming = {
            "company_name": "Flipkart",
            "role": "Assistant Manager NEEV",
            "current_status": "OA",
        }
        opp_id, created = db_manager.insert_or_update_opportunity(
            incoming,
            source_email_id="flipkart_oa_ambiguous",
            email_classification="OA_UPDATE",
        )

        after = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        assert opp_id is None
        assert created is False
        assert after == before  # attached to neither, created nothing

        review_rows = db_manager.fetch_unmatched_confirmations()
        assert len(review_rows) == 1
        assert review_rows[0]["gmail_message_id"] == "flipkart_oa_ambiguous"
        candidate_ids = {c["id"] for c in review_rows[0]["candidates"]}
        assert candidate_ids == {ppo_id, intern_id}

    def test_ambiguous_candidates_without_source_email_id_still_blocks_attach(
        self, db_manager
    ):
        """No gmail_message_id to key a review row on -- must still refuse
        to guess, just skip writing to unmatched_confirmations."""
        id_a = _seed(db_manager, company_name="Honeywell", role="Super Dream Internship")
        id_b = _seed(
            db_manager,
            company_name="Honeywell",
            role="Aerospace Placement Drive",
            source_email_id="seed-Honeywell-2",
        )
        assert id_a != id_b

        incoming = {"company_name": "Honeywell", "role": "PPT & Online Test"}
        opp_id, created = db_manager.insert_or_update_opportunity(
            incoming, source_email_id=None, email_classification="OA_UPDATE"
        )

        assert opp_id is None
        assert created is False
        assert db_manager.fetch_unmatched_confirmations() == []


class TestProgramNameTieBreak:
    def test_compatible_program_name_narrows_to_one_candidate(self, db_manager):
        """Two candidates exist, but the incoming role repeats one of their
        program names verbatim -- narrows to exactly one, attaches."""
        ppo_id = _seed(db_manager, company_name="Flipkart", role="Super Dream PPO")
        intern_id = _seed(
            db_manager,
            company_name="Flipkart",
            role="Super Dream Internship",
            source_email_id="seed-Flipkart-intern",
        )

        incoming = {
            "company_name": "Flipkart",
            "role": "Super Dream Internship - Online test is scheduled",
        }
        opp_id, created = db_manager.insert_or_update_opportunity(
            incoming,
            source_email_id="flipkart_intern_oa",
            email_classification="OA_UPDATE",
        )

        assert created is False
        assert opp_id == intern_id
        assert opp_id != ppo_id


class TestDriveIdSuffixRemoved:
    """Cause 1: the ``_02`` sequence-suffix logic in ``_insert_opportunity``
    force-created duplicate *opportunity rows* through the uniqueness
    constraint and had to go. A side effect, flagged here rather than
    silently accepted: ``drive_id`` itself has no UNIQUE constraint and is
    only human-readable, so two genuinely distinct new drives at the same
    company/year whose first three role words coincide can now share a
    generated ``drive_id``. That is a known, narrow consequence of the
    plan's required fix, not a goal -- ``set_my_status`` was hardened
    alongside this removal to scope its write to a single row even when
    ``drive_id`` collides, so the collision can no longer corrupt two
    drives' ``my_status`` in one write."""

    def test_two_new_drives_sharing_a_drive_id_prefix_get_no_forced_suffix(self, db_manager):
        first = {
            "company_name": "Acme Corp",
            "role": "Software Engineer Intern Bangalore",
            "current_status": "OPEN",
        }
        second = {
            "company_name": "Acme Corp",
            "role": "Software Engineer Intern Chennai",
            "current_status": "OPEN",
        }
        id1, created1 = db_manager.insert_or_update_opportunity(
            first, source_email_id="acme_1"
        )
        id2, created2 = db_manager.insert_or_update_opportunity(
            second, source_email_id="acme_2"
        )

        assert created1 is True
        assert created2 is True
        assert id1 != id2

        drive_id_1 = db_manager.fetch_opportunity_by_id(id1)["drive_id"]
        drive_id_2 = db_manager.fetch_opportunity_by_id(id2)["drive_id"]
        # Same first-3-words role slug -> identical generated drive_id, and
        # crucially no "_02"/"_03" suffix appended to force uniqueness --
        # that suffix was what force-created duplicate opportunity rows.
        assert drive_id_1 == drive_id_2
        assert not drive_id_2.endswith("_02")

    def test_colliding_drive_id_my_status_write_touches_only_one_row(self, db_manager):
        """The collision documented above must not become a data-integrity
        bug: set_my_status must never write two rows from one call."""
        first = {
            "company_name": "Acme Corp",
            "role": "Software Engineer Intern Bangalore",
            "current_status": "OPEN",
        }
        second = {
            "company_name": "Acme Corp",
            "role": "Software Engineer Intern Chennai",
            "current_status": "OPEN",
        }
        id1, _ = db_manager.insert_or_update_opportunity(first, source_email_id="acme_1")
        id2, _ = db_manager.insert_or_update_opportunity(second, source_email_id="acme_2")
        drive_id = db_manager.fetch_opportunity_by_id(id2)["drive_id"]
        assert db_manager.fetch_opportunity_by_id(id1)["drive_id"] == drive_id

        changed = db_manager.set_my_status(drive_id, "APPLIED", source="sheet")
        assert changed is True

        statuses = {
            id1: db_manager.fetch_opportunity_by_id(id1)["my_status"],
            id2: db_manager.fetch_opportunity_by_id(id2)["my_status"],
        }
        applied_count = sum(1 for status in statuses.values() if status == "APPLIED")
        assert applied_count == 1, (
            "set_my_status must scope its write to a single row even when "
            f"drive_id collides; got {statuses}"
        )


class TestExpiredDriveStillResolvable:
    """docs/design/16 Cause 1 follow-up, found while dry-running the Phase 6
    confirmations backfill against live data: current_status=EXPIRED means
    the registration *window* closed, not that the drive's process ended.
    97 of 215 live opportunities sat at EXPIRED, and every one was invisible
    to the old ACTIVE_CURRENT_STATUSES-scoped resolver -- Accenture alone
    would have minted 11 phantom duplicate rows from 11 confirmation mails
    for a drive (id=243) that already existed. The resolver must use
    RESOLVER_CANDIDATE_STATUSES (ACTIVE_CURRENT_STATUSES + EXPIRED), while
    fetch_active_drives_only (dashboard display) keeps excluding EXPIRED."""

    def test_confirmation_mail_attaches_to_expired_drive_not_creates(self, db_manager):
        seeded_id = _seed(
            db_manager, company_name="Accenture", current_status="OPEN"
        )
        db_manager.connection.execute(
            "UPDATE opportunities SET current_status = 'EXPIRED' WHERE id = ?;",
            (seeded_id,),
        )
        db_manager.connection.commit()
        before = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        incoming = {"company_name": "Accenture", "role": ""}
        opp_id, created = db_manager.insert_or_update_opportunity(
            incoming,
            source_email_id="accenture_confirmation",
            email_classification="APPLICATION_CONFIRMATION",
        )

        after = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        assert opp_id == seeded_id
        assert created is False
        assert after == before

    def test_selected_and_open_candidates_still_both_win_over_nothing(self, db_manager):
        """Sanity: the widened pool doesn't change behaviour for drives that
        were already resolvable -- only EXPIRED is newly included."""
        seeded_id = _seed(db_manager, company_name="Foodhub", current_status="OA")
        incoming = {"company_name": "Foodhub", "role": ""}
        opp_id, created = db_manager.insert_or_update_opportunity(
            incoming,
            source_email_id="foodhub_confirmation",
            email_classification="APPLICATION_CONFIRMATION",
        )
        assert opp_id == seeded_id
        assert created is False
