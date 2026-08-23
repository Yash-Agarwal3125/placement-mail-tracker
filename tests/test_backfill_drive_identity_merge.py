"""Tests for scripts/backfill_drive_identity_merge.py (Phase 2, calendar-drift
remediation plan): the one-off collapse of drives fragmented by Cause 1's
role-hash instability.

Never touches the live database -- operates on the in-memory ``db_manager``
fixture only, per CLAUDE.md and the plan's explicit "build and unit-test
only" constraint.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backfill_drive_identity_merge as merge_script  # noqa: E402


def _seed(db_manager, **overrides):
    base = {
        "company_name": "Honeywell",
        "role": "Intern",
        "current_status": "OPEN",
        "eligibility_status": "ELIGIBLE",
        "email_received_at": "17-Aug-2026 10:00 AM",
    }
    base.update(overrides)
    opp_id, _ = db_manager.insert_or_update_opportunity(
        base, source_email_id=overrides.get("source_email_id", f"seed-{base['role']}")
    )
    return opp_id


class TestGrouping:
    def test_groups_same_company_year_active_placement_rows(self, db_manager):
        id1 = _seed(db_manager, role="Intern", source_email_id="h1")
        id2 = _seed(
            db_manager,
            role="Intern role=Intern type=internship_and_fulltime",
            source_email_id="h2",
        )
        _other_company = _seed(db_manager, company_name="Blackrock", source_email_id="b1")

        rows = [
            dict(r)
            for r in db_manager.connection.execute("SELECT * FROM opportunities").fetchall()
        ]
        groups = merge_script.group_candidates(rows)

        assert len(groups) == 1
        (key, members), = groups.items()
        assert key[0] == "Honeywell"
        member_ids = {m["id"] for m in members}
        assert member_ids == {id1, id2}

    def test_non_placement_drive_kind_excluded_from_grouping(self, db_manager):
        """GRiD 8.0 (a contest, drive_kind != PLACEMENT) must never be
        pulled into the same cluster as a real placement drive."""
        _real_drive = _seed(db_manager, company_name="Flipkart", role="Super Dream PPO")
        grid_id = _seed(
            db_manager, company_name="Flipkart", role="GRiD 8.0", source_email_id="grid"
        )
        db_manager.connection.execute(
            "UPDATE opportunities SET drive_kind = 'HACKATHON' WHERE id = ?;",
            (grid_id,),
        )

        rows = [
            dict(r)
            for r in db_manager.connection.execute("SELECT * FROM opportunities").fetchall()
        ]
        groups = merge_script.group_candidates(rows)
        assert groups == {}

    def test_exclude_id_pulls_a_row_out_of_its_cluster(self, db_manager):
        _seed(db_manager, role="Super Dream PPO", source_email_id="fp1")
        id2 = _seed(db_manager, role="Super Dream Internship", source_email_id="fp2")

        rows = [
            dict(r)
            for r in db_manager.connection.execute("SELECT * FROM opportunities").fetchall()
        ]
        groups = merge_script.group_candidates(rows, exclude_ids={id2})
        assert groups == {}  # only one member left -> not a mergeable cluster

        (key, members), = merge_script.group_candidates(rows).items()  # sanity: normally 2
        assert len(members) == 2
        assert key


class TestPlanMerges:
    def test_plan_prints_roles_dates_and_subjects_without_writing(self, db_manager):
        id1 = _seed(db_manager, role="Intern", deadline="1 Sep 2026", source_email_id="h1")
        id2 = _seed(
            db_manager, role="Super Dream Internship", oa_date="5 Sep 2026", source_email_id="h2"
        )
        db_manager.log_processed_email(
            gmail_message_id="h1", subject="Honeywell registration open",
            opportunity_id=id1,
        )
        db_manager.log_processed_email(
            gmail_message_id="h2", subject="Honeywell OA scheduled", opportunity_id=id2,
        )

        before_count = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]

        plans = merge_script.plan_merges(db_manager)

        after_count = db_manager.connection.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]
        assert after_count == before_count  # dry-run never writes

        assert len(plans) == 1
        plan = plans[0]
        assert plan["survivor_id"] in (id1, id2)
        assert set(plan["loser_ids"]) == {id1, id2} - {plan["survivor_id"]}
        subjects = {s for m in plan["members"] for s in m["subjects"]}
        assert "Honeywell registration open" in subjects
        assert "Honeywell OA scheduled" in subjects


class TestApplyMerges:
    def test_apply_repoints_every_foreign_key_not_cascades(self, db_manager):
        survivor_id = _seed(
            db_manager, role="Intern", source_email_id="h1", my_status="APPLIED"
        )
        loser_id = _seed(
            db_manager,
            role="Super Dream Internship",
            source_email_id="h2",
            my_status="NOT_APPLIED",
        )
        # Force a deterministic survivor: give the loser the extra field so
        # it would "win" on population count, then re-check by id afterward
        # rather than assuming which one survives.
        db_manager.upsert_roster_verdict(loser_id, "OA", "MATCHED", method="registration_no")
        db_manager.create_update_event(loser_id, "note", notes="loser update row")
        db_manager.log_processed_email(
            gmail_message_id="h2-mail", subject="Honeywell OA", opportunity_id=loser_id,
        )

        counts_before = {
            table: db_manager.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("roster_verdicts", "updates", "processed_emails")
        }

        stats = merge_script.apply_merges(db_manager)
        assert stats["clusters_merged"] == 1
        assert stats["rows_absorbed"] == 1

        counts_after = {
            table: db_manager.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("roster_verdicts", "updates", "processed_emails")
        }
        # roster_verdicts row count unchanged (re-pointed, not cascaded away)
        assert counts_after["roster_verdicts"] == counts_before["roster_verdicts"]
        # processed_emails re-pointed, never deleted
        assert counts_after["processed_emails"] == counts_before["processed_emails"]
        # updates gains the "merged" audit row on top of whatever existed
        assert counts_after["updates"] == counts_before["updates"] + 1

        remaining = db_manager.connection.execute(
            "SELECT id FROM opportunities"
        ).fetchall()
        assert len(remaining) == 1
        survivor = remaining[0]["id"]
        assert survivor in (survivor_id, loser_id)

        # The roster verdict that was on the loser now points at whichever
        # row survived.
        verdict_row = db_manager.connection.execute(
            "SELECT opportunity_id FROM roster_verdicts WHERE event_type = 'OA'"
        ).fetchone()
        assert verdict_row["opportunity_id"] == survivor

        pe_row = db_manager.connection.execute(
            "SELECT opportunity_id FROM processed_emails WHERE gmail_message_id = 'h2-mail'"
        ).fetchone()
        assert pe_row["opportunity_id"] == survivor

    def test_apply_commits_explicitly(self, db_manager):
        """A standalone script has nothing else in-process to flush the
        transaction -- apply_merges must call commit() itself."""
        _seed(db_manager, role="Intern", source_email_id="h1")
        _seed(db_manager, role="Super Dream Internship", source_email_id="h2")

        merge_script.apply_merges(db_manager)

        # in_transaction is False right after an explicit commit().
        assert db_manager.connection.in_transaction is False

    def test_my_status_ladder_never_downgrades_on_merge(self, db_manager):
        survivor_candidate_a = _seed(
            db_manager, role="Intern", source_email_id="h1", my_status="NOT_APPLIED"
        )
        survivor_candidate_b = _seed(
            db_manager,
            role="Super Dream Internship",
            source_email_id="h2",
            my_status="SELECTED",
        )

        merge_script.apply_merges(db_manager)

        remaining = db_manager.connection.execute(
            "SELECT my_status FROM opportunities"
        ).fetchone()
        assert remaining["my_status"] == "SELECTED"
        assert survivor_candidate_a and survivor_candidate_b  # both existed pre-merge

    def test_user_asserted_verdict_dropped_when_real_verdict_exists(self, db_manager):
        id1 = _seed(db_manager, role="Intern", source_email_id="h1")
        id2 = _seed(db_manager, role="Super Dream Internship", source_email_id="h2")

        db_manager.upsert_roster_verdict(
            id1, "INTERVIEW", "NOT_MATCHED", method="user_asserted"
        )
        db_manager.upsert_roster_verdict(
            id2, "INTERVIEW", "MATCHED", method="registration_no"
        )

        merge_script.apply_merges(db_manager)

        verdict_row = db_manager.connection.execute(
            "SELECT verdict, method FROM roster_verdicts WHERE event_type = 'INTERVIEW'"
        ).fetchone()
        assert verdict_row["method"] == "registration_no"
        assert verdict_row["verdict"] == "MATCHED"

    def test_calendar_events_deduped_one_per_event_type(self, db_manager):
        id1 = _seed(db_manager, role="Intern", source_email_id="h1")
        id2 = _seed(db_manager, role="Super Dream Internship", source_email_id="h2")

        from placement_mail_tracker.calendar_sync.derive import CalendarEvent

        db_manager.upsert_calendar_event_state(
            CalendarEvent(
                opportunity_id=id1, drive_id="D1", event_type="OA", title="t1",
                start_iso="2026-09-01", end_iso="2026-09-01", all_day=True,
                location=None, description="", reminder_minutes=[],
            ),
            gcal_calendar_id="cal", gcal_event_id="evt-1", status="active",
        )
        db_manager.upsert_calendar_event_state(
            CalendarEvent(
                opportunity_id=id2, drive_id="D2", event_type="OA", title="t2",
                start_iso="2026-09-05", end_iso="2026-09-05", all_day=True,
                location=None, description="", reminder_minutes=[],
            ),
            gcal_calendar_id="cal", gcal_event_id="evt-2", status="active",
        )

        merge_script.apply_merges(db_manager)

        oa_states = db_manager.connection.execute(
            "SELECT * FROM calendar_events WHERE event_type = 'OA'"
        ).fetchall()
        assert len(oa_states) == 1

    def test_apply_without_apply_flag_never_called_leaves_db_untouched(self, db_manager):
        """Sanity check on the CLI contract: plan_merges (the --dry-run path)
        never mutates, apply_merges (the --apply path) does."""
        _seed(db_manager, role="Intern", source_email_id="h1")
        _seed(db_manager, role="Super Dream Internship", source_email_id="h2")

        before = [
            dict(r) for r in db_manager.connection.execute("SELECT * FROM opportunities")
        ]
        merge_script.plan_merges(db_manager)
        after = [
            dict(r) for r in db_manager.connection.execute("SELECT * FROM opportunities")
        ]
        assert before == after
