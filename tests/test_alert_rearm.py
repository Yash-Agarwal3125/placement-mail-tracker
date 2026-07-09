"""ADR-D8 / B3: alert dedup must re-arm after a reschedule.

sent_alerts is keyed UNIQUE(opportunity_id, alert_type). Without a
date-suffixed alert_type, an OA rescheduled from June 10 to June 17 would
never get its EVENT_24H alert re-sent, because June 10's EVENT_24H already
burned the key.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock

from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.scheduler.alert_generator import AlertGenerator


def _make_alert_generator(db_manager: DatabaseManager) -> AlertGenerator:
    settings = Mock()
    settings.smtp_email = "test@gmail.com"
    settings.smtp_app_password = "password"
    settings.notification_email = "notify@gmail.com"
    settings.email_receiver = "recv@gmail.com"
    gen = AlertGenerator(db_manager, settings)
    gen.notifier = Mock()
    return gen


class TestDeadlineAlertRearm:
    def test_reschedule_to_a_new_date_fires_again(self, db_manager: DatabaseManager):
        gen = _make_alert_generator(db_manager)

        # Sync run 1: deadline is 3h out (DEADLINE_4H), fires once.
        now1 = datetime(2026, 1, 1, 12, 0, 0)
        opp = {
            "id": 1, "company_name": "Test Co",
            "deadline": (now1 + timedelta(hours=3)).isoformat(),
        }
        gen._check_deadline_alerts(opp, now1)
        assert gen.notifier.send_email.call_count == 1

        # Same run checked again with nothing changed: dedup applies.
        gen._check_deadline_alerts(opp, now1)
        assert gen.notifier.send_email.call_count == 1

        # College reschedules the deadline to the next day; a later sync run
        # (now2, a day on) finds it back within the same DEADLINE_4H window —
        # this must re-fire, not stay silenced by the old date's alert key.
        now2 = datetime(2026, 1, 2, 12, 0, 0)
        opp_rescheduled = {**opp, "deadline": (now2 + timedelta(hours=3)).isoformat()}
        gen._check_deadline_alerts(opp_rescheduled, now2)
        assert gen.notifier.send_email.call_count == 2


class TestEventAlertRearm:
    def test_reschedule_to_a_new_date_fires_again(self, db_manager: DatabaseManager):
        gen = _make_alert_generator(db_manager)

        now1 = datetime(2026, 1, 1, 12, 0, 0)
        opp = {
            "id": 2, "company_name": "Test Co",
            "next_event_date": (now1 + timedelta(hours=20)).isoformat(),
        }
        gen._check_event_alerts(opp, now1)
        assert gen.notifier.send_email.call_count == 1

        gen._check_event_alerts(opp, now1)
        assert gen.notifier.send_email.call_count == 1

        now2 = datetime(2026, 1, 8, 12, 0, 0)
        opp_rescheduled = {
            **opp, "next_event_date": (now2 + timedelta(hours=20)).isoformat(),
        }
        gen._check_event_alerts(opp_rescheduled, now2)
        assert gen.notifier.send_email.call_count == 2
