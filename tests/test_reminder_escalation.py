"""Feature 2 (docs/design/10-confirmation-and-reminders.md): batched T-48h/
T-24h deadline-escalation alerts.

Batching and dedup are different concerns: sent_alerts keeps one row per
qualifying drive (UNIQUE(opportunity_id, alert_type)) so per-drive re-arm-on-
reschedule still works -- only the *send* is batched into one mail per run
per alert_type.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.scheduler.alert_generator import AlertGenerator


def _opp(id_, company, hours_left, now, *, my_status="NOT_APPLIED", eligibility="ELIGIBLE",
         validation_flags=None, registration_link=None):
    return {
        "id": id_,
        "company_name": company,
        "role": "SDE",
        "eligibility_status": eligibility,
        "my_status": my_status,
        "deadline": (now + timedelta(hours=hours_left)).isoformat(),
        "validation_flags": validation_flags or [],
        "registration_link": registration_link,
    }


class TestDeadlineEscalationFiresAndDedups:
    def test_fires_at_48h_and_24h_and_dedups_on_rerun(
        self, db_manager: DatabaseManager, mock_settings
    ):
        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()
        gen.notifier.send_email.return_value = True

        now = datetime(2026, 7, 10, 6, 0, 0)
        opp = _opp(7, "Unapplied Co", 30, now)
        buckets: dict = {}
        gen._collect_deadline_escalation_candidate(opp, now, buckets)
        gen._send_batched_deadline_escalations(buckets)

        assert gen.notifier.send_email.call_count == 1
        subject_48h = gen.notifier.send_email.call_args[1]["subject"]
        assert "T-48h" in subject_48h

        # Rerun with nothing changed: dedup applies via sent_alerts, no resend.
        buckets = {}
        gen._collect_deadline_escalation_candidate(opp, now, buckets)
        assert buckets == {}

        # Time advances into the tighter 24h band -> a second, distinct alert.
        later = now + timedelta(hours=10)  # 20h left
        opp_later = {**opp, "deadline": (now + timedelta(hours=30)).isoformat()}
        buckets = {}
        gen._collect_deadline_escalation_candidate(opp_later, later, buckets)
        gen._send_batched_deadline_escalations(buckets)
        assert gen.notifier.send_email.call_count == 2
        subject_24h = gen.notifier.send_email.call_args[1]["subject"]
        assert "T-24h" in subject_24h

    def test_does_not_fire_for_already_applied_drive(
        self, db_manager: DatabaseManager, mock_settings
    ):
        gen = AlertGenerator(db_manager, mock_settings)
        now = datetime(2026, 7, 10, 6, 0, 0)
        opp = _opp(8, "Applied Co", 30, now, my_status="REGISTERED")

        buckets: dict = {}
        gen._collect_deadline_escalation_candidate(opp, now, buckets)

        assert buckets == {}

    def test_reschedule_to_a_new_date_re_arms(self, db_manager: DatabaseManager, mock_settings):
        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()
        gen.notifier.send_email.return_value = True

        now = datetime(2026, 7, 10, 6, 0, 0)
        opp = _opp(9, "Reschedule Co", 30, now)
        buckets: dict = {}
        gen._collect_deadline_escalation_candidate(opp, now, buckets)
        gen._send_batched_deadline_escalations(buckets)
        assert gen.notifier.send_email.call_count == 1

        rescheduled = _opp(9, "Reschedule Co", 30, now + timedelta(days=3))
        buckets = {}
        gen._collect_deadline_escalation_candidate(rescheduled, now + timedelta(days=3), buckets)
        gen._send_batched_deadline_escalations(buckets)
        assert gen.notifier.send_email.call_count == 2


class TestDeadlineEscalationBatching:
    def test_two_drives_different_tiers_produce_two_mails(
        self, db_manager: DatabaseManager, mock_settings, sample_opportunity
    ):
        opp_a_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Alpha Co", "SDE"), source_email_id="alpha_seed",
        )
        opp_b_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Beta Co", "SDE"), source_email_id="beta_seed",
        )

        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()
        gen.notifier.send_email.return_value = True

        now = datetime(2026, 7, 10, 6, 0, 0)
        opp_a = _opp(opp_a_id, "Alpha Co", 30, now)  # lands in the T-48h tier
        opp_b = _opp(opp_b_id, "Beta Co", 20, now)  # lands in the T-24h tier

        buckets: dict = {}
        gen._collect_deadline_escalation_candidate(opp_a, now, buckets)
        gen._collect_deadline_escalation_candidate(opp_b, now, buckets)
        gen._send_batched_deadline_escalations(buckets)

        # Two distinct alert_type buckets (T48 for Alpha, T24 for Beta) -> two mails.
        assert gen.notifier.send_email.call_count == 2

        sent_rows = db_manager.connection.execute(
            "SELECT COUNT(*) FROM sent_alerts WHERE opportunity_id IN (?, ?)",
            (opp_a_id, opp_b_id),
        ).fetchone()[0]
        assert sent_rows == 2

    def test_two_drives_crossing_same_tier_batch_into_one_mail(
        self, db_manager: DatabaseManager, mock_settings, sample_opportunity
    ):
        opp_a_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Gamma Co", "SDE"), source_email_id="gamma_seed",
        )
        opp_b_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Delta Co", "SDE"), source_email_id="delta_seed",
        )

        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()
        gen.notifier.send_email.return_value = True

        now = datetime(2026, 7, 10, 6, 0, 0)
        opp_a = _opp(opp_a_id, "Gamma Co", 40, now)
        opp_b = _opp(opp_b_id, "Delta Co", 35, now)  # both land in the T-48h tier

        buckets: dict = {}
        gen._collect_deadline_escalation_candidate(opp_a, now, buckets)
        gen._collect_deadline_escalation_candidate(opp_b, now, buckets)
        gen._send_batched_deadline_escalations(buckets)

        assert gen.notifier.send_email.call_count == 1
        body = gen.notifier.send_email.call_args[1]["body"]
        assert "Gamma Co" in body
        assert "Delta Co" in body

        sent_rows = db_manager.connection.execute(
            "SELECT COUNT(*) FROM sent_alerts WHERE opportunity_id IN (?, ?)",
            (opp_a_id, opp_b_id),
        ).fetchone()[0]
        assert sent_rows == 2

    def test_overflow_beyond_cap_is_not_marked_sent(
        self, db_manager: DatabaseManager, mock_settings, sample_opportunity
    ):
        settings = mock_settings.model_copy(update={"reminder_max_per_mail": 1})
        gen = AlertGenerator(db_manager, settings)
        gen.notifier = MagicMock()
        gen.notifier.send_email.return_value = True

        opp_a_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("First Co", "SDE"), source_email_id="first_seed",
        )
        opp_b_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Second Co", "SDE"), source_email_id="second_seed",
        )
        now = datetime(2026, 7, 10, 6, 0, 0)
        opp_a = _opp(opp_a_id, "First Co", 10, now)  # more urgent -> included
        opp_b = _opp(opp_b_id, "Second Co", 15, now)  # same T-24h tier, same calendar day

        buckets: dict = {}
        gen._collect_deadline_escalation_candidate(opp_a, now, buckets)
        gen._collect_deadline_escalation_candidate(opp_b, now, buckets)
        gen._send_batched_deadline_escalations(buckets)

        assert gen.notifier.send_email.call_count == 1
        body = gen.notifier.send_email.call_args[1]["body"]
        assert "First Co" in body
        assert "+1 more" in body

        sent_ids = {
            row[0] for row in db_manager.connection.execute(
                "SELECT opportunity_id FROM sent_alerts WHERE opportunity_id IN (?, ?)",
                (opp_a_id, opp_b_id),
            ).fetchall()
        }
        assert sent_ids == {opp_a_id}


class TestDeadlineEscalationFlaggedExclusion:
    def test_flagged_deadline_is_excluded_from_escalation(
        self, db_manager: DatabaseManager, mock_settings
    ):
        gen = AlertGenerator(db_manager, mock_settings)
        now = datetime(2026, 7, 10, 6, 0, 0)
        opp = _opp(
            10, "Flagged Co", 30, now,
            validation_flags=["deadline value '15 July' only parses under fuzzy date matching"],
        )

        buckets: dict = {}
        gen._collect_deadline_escalation_candidate(opp, now, buckets)

        assert buckets == {}

    def test_several_flagged_deadlines_at_once_produce_zero_escalation_sends(
        self, db_manager: DatabaseManager, mock_settings
    ):
        """Readiness-at-volume check: the single-drive test above proves the
        exclusion at the unit level; this drives several flagged rows through
        the full check_and_send_alerts() pipeline at once (not one row in
        isolation) to prove no batched DEADLINE_T48/T24 mail goes out for any
        of them, and that a mix of flagged + clean drives in the same run
        only escalates the clean one."""
        gen = AlertGenerator(db_manager, mock_settings)
        gen.notifier = MagicMock()
        gen.notifier.send_email.return_value = True
        # check_and_send_alerts() computes its own datetime.now() internally
        # (no injectable clock), so deadlines must be relative to real now.
        now = datetime.now()

        flagged_opps = [
            _opp(
                100 + i, f"Flagged Co {i}", 30, now,
                validation_flags=[f"deadline value looks implausible ({i}) — verify manually"],
            )
            for i in range(3)
        ]
        clean_opp = _opp(200, "Clean Co", 30, now)

        gen.database.fetch_active_opportunities = MagicMock(
            return_value=[*flagged_opps, clean_opp]
        )
        gen.check_and_send_alerts()

        # Exactly one batched escalation mail (the clean drive); the 3
        # flagged drives contribute zero DEADLINE_T48/T24 sends.
        escalation_calls = [
            c for c in gen.notifier.send_email.call_args_list
            if "T-48h" in c.kwargs.get("subject", "")
        ]
        assert len(escalation_calls) == 1
        assert "Clean Co" in escalation_calls[0].kwargs["body"]
        for flagged in flagged_opps:
            assert flagged["company_name"] not in escalation_calls[0].kwargs["body"]

        sent_ids = {
            row[0] for row in db_manager.connection.execute(
                "SELECT opportunity_id FROM sent_alerts WHERE alert_type LIKE 'DEADLINE_T48:%'"
            ).fetchall()
        }
        assert sent_ids == {200}


class TestReminderEscalationDisabled:
    def test_disabled_flag_skips_escalation_entirely(
        self, db_manager: DatabaseManager, mock_settings, sample_opportunity
    ):
        """Only the escalation batch is gated by this flag -- the generic
        deadline/event alerts (pre-existing, unrelated to this feature) may
        still fire for the same drive, so assert on sent_alerts rows
        specifically rather than on send_email never being called at all."""
        settings = mock_settings.model_copy(update={"reminder_escalation_enabled": False})
        opp_id, _ = db_manager.insert_or_update_opportunity(
            sample_opportunity("Nudge Co", "SDE"), source_email_id="nudge_seed",
        )
        db_manager.connection.execute(
            "UPDATE opportunities SET eligibility_status = 'ELIGIBLE', deadline = ? WHERE id = ?",
            ((datetime.now() + timedelta(hours=30)).isoformat(), opp_id),
        )
        db_manager.connection.commit()

        gen = AlertGenerator(db_manager, settings)
        gen.notifier = MagicMock()
        gen.notifier.send_email.return_value = True
        gen.check_and_send_alerts()

        sent_types = {
            row[0] for row in db_manager.connection.execute(
                "SELECT alert_type FROM sent_alerts WHERE opportunity_id = ?", (opp_id,)
            ).fetchall()
        }
        assert not any(t.startswith("DEADLINE_T") for t in sent_types)
