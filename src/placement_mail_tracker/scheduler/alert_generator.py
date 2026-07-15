"""Smart alerting logic for upcoming deadlines and events."""

import logging
from datetime import datetime, timedelta
from typing import Any

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.notifications.email_notifier import EmailNotifier
from placement_mail_tracker.utils.time import parse_datetime_flexible, utc_now_iso

logger = logging.getLogger(__name__)

# Placeholder company values that mean extraction failed — never alert on these.
_UNIDENTIFIED_COMPANIES = frozenset({"", "unknown", "unknown company"})


def _is_identifiable(opp: dict[str, Any]) -> bool:
    """Return True when the drive has a real company name to act on."""
    name = opp.get("company_name")
    return bool(name) and str(name).strip().casefold() not in _UNIDENTIFIED_COMPANIES


class AlertGenerator:
    """Generates smart notifications based on proximity to deadlines/events."""
    
    def __init__(self, database: DatabaseManager, settings: Settings):
        self.database = database
        self.settings = settings
        self.notifier = EmailNotifier(settings)
        
    def check_and_send_alerts(self) -> None:
        """Scan active opportunities and trigger alerts if deadlines/events are approaching."""
        now = datetime.now()
        active_opps = self.database.fetch_active_opportunities()
        escalation_buckets: dict[str, list[dict[str, Any]]] = {}

        for opp in active_opps:
            # Skip drives we can't attribute to a real company — an
            # "Unknown Deadline in <11 hours" alert is noise, not signal.
            if not _is_identifiable(opp):
                continue

            # Only send alerts if eligible
            if opp.get("eligibility_status") not in {"ELIGIBLE", "MANUAL_REVIEW"}:
                continue

            self._check_deadline_alerts(opp, now)
            self._check_event_alerts(opp, now)

            if self.settings.reminder_escalation_enabled:
                self._collect_deadline_escalation_candidate(opp, now, escalation_buckets)

        if escalation_buckets:
            self._send_batched_deadline_escalations(escalation_buckets)

    def _check_deadline_alerts(self, opp: dict[str, Any], now: datetime) -> None:
        """Feature 2: Deadline alerts at <24h and <4h."""
        deadline_str = opp.get("deadline")
        if not deadline_str:
            return
            
        deadline_dt = parse_datetime_flexible(deadline_str)
        if not deadline_dt:
            return
            
        time_left = deadline_dt - now
        
        # Don't alert if deadline passed
        if time_left < timedelta(0):
            return
            
        hours_left = time_left.total_seconds() / 3600
        
        alert_type = None
        if hours_left <= 4:
            alert_type = "DEADLINE_4H"
        elif hours_left <= 24:
            alert_type = "DEADLINE_24H"

        # Date-suffix the key so a reschedule (deadline moves to a new date)
        # re-arms the alert instead of staying permanently burned against the
        # UNIQUE(opportunity_id, alert_type) constraint (ADR-D8 / B3).
        if alert_type:
            alert_type = f"{alert_type}:{deadline_dt.date().isoformat()}"

        if alert_type and self._should_send_alert(opp["id"], alert_type):
            logger.info("Sending %s alert for %s", alert_type, opp["company_name"])
            self._send_alert_email(
                opp, 
                subject=f"⚠️ URGENT: {opp['company_name']} Deadline in <{int(hours_left)} hours",
                alert_type=alert_type,
                time_left=f"{int(hours_left)} hours"
            )
            self._mark_alert_sent(opp["id"], alert_type)

    def _check_event_alerts(self, opp: dict[str, Any], now: datetime) -> None:
        """Feature 3: Event alerts at <48h, <24h, and <4h.

        Iterates oa_date and interview_date directly (mirroring
        sheets_sync._build_upcoming_events) instead of the single derived
        next_event_date, which only ever holds the earliest event — once
        that one passes, a drive's second event (e.g. an interview after an
        OA) previously got zero push alerts. The dedup key is suffixed with
        the field name so the two dates never collide on the same alert_type.
        """
        for field_name, label in (("oa_date", "OA"), ("interview_date", "Interview")):
            event_str = opp.get(field_name)
            if not event_str:
                continue

            event_dt = parse_datetime_flexible(event_str)
            if not event_dt:
                continue

            time_left = event_dt - now
            if time_left < timedelta(0):
                continue

            hours_left = time_left.total_seconds() / 3600

            alert_type = None
            if hours_left <= 4:
                alert_type = "EVENT_4H"
            elif hours_left <= 24:
                alert_type = "EVENT_24H"
            elif hours_left <= 48:
                alert_type = "EVENT_48H"

            if not alert_type:
                continue

            # Date- and field-suffix the key so a reschedule re-arms the
            # alert (ADR-D8 / B3) and the OA/interview dates never share a key.
            alert_type = f"{alert_type}:{field_name}:{event_dt.date().isoformat()}"

            if self._should_send_alert(opp["id"], alert_type):
                logger.info("Sending %s alert for %s", alert_type, opp["company_name"])
                self._send_alert_email(
                    opp,
                    subject=(
                        f"📅 UPCOMING {label.upper()}: {opp['company_name']} "
                        f"in <{int(hours_left)} hours"
                    ),
                    alert_type=alert_type,
                    time_left=f"{int(hours_left)} hours",
                )
                self._mark_alert_sent(opp["id"], alert_type)

    def _collect_deadline_escalation_candidate(
        self, opp: dict[str, Any], now: datetime, buckets: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Backlog item 1 (docs/design/05, promoted in 09; batched shape locked
        in docs/design/10-confirmation-and-reminders.md Feature 2): collect
        ELIGIBLE + NOT_APPLIED drives whose deadline is closing in, so the
        caller can send ONE batched mail per alert_type instead of a storm of
        per-drive mails. Drives whose deadline the validation layer already
        distrusts are excluded here — they're listed once in the digest as
        "deadline unverified" instead of alarming on data we don't trust.
        """
        if opp.get("eligibility_status") != "ELIGIBLE" or (
            opp.get("my_status") or "NOT_APPLIED"
        ) != "NOT_APPLIED":
            return

        deadline_str = opp.get("deadline")
        if not deadline_str:
            return

        validation_flags = opp.get("validation_flags") or []
        if any(str(flag).startswith("deadline ") for flag in validation_flags):
            return

        deadline_dt = parse_datetime_flexible(deadline_str)
        if not deadline_dt:
            return

        time_left = deadline_dt - now
        if time_left < timedelta(0):
            return

        hours_left = time_left.total_seconds() / 3600
        thresholds = sorted(self.settings.deadline_escalation_thresholds_hours)

        tier_hours = None
        for threshold in thresholds:
            if hours_left <= threshold:
                tier_hours = threshold
                break
        if tier_hours is None:
            return

        alert_type = f"DEADLINE_T{tier_hours}:{deadline_dt.date().isoformat()}"
        if not self._should_send_alert(opp["id"], alert_type):
            return

        buckets.setdefault(alert_type, []).append(
            {"opp": opp, "hours_left": hours_left, "tier_hours": tier_hours}
        )

    def _send_batched_deadline_escalations(self, buckets: dict[str, list[dict[str, Any]]]) -> None:
        """One mail per alert_type per run (feature_2_spec) — no per-drive
        mail storms — while sent_alerts keeps its existing UNIQUE(opportunity_
        id, alert_type) per-drive rows, so re-arm-on-reschedule still works
        per drive."""
        cap = max(1, self.settings.reminder_max_per_mail)

        for alert_type, items in buckets.items():
            items.sort(key=lambda i: i["hours_left"])
            included = items[:cap]
            overflow = len(items) - len(included)
            tier_hours = included[0]["tier_hours"]

            rows = []
            for item in included:
                opp = item["opp"]
                link = opp.get("registration_link")
                apply_cell = f'<a href="{link}">Apply</a>' if link else "N/A"
                rows.append(
                    "<tr>"
                    f"<td>{opp.get('company_name')}</td>"
                    f"<td>{opp.get('role')}</td>"
                    f"<td>{int(item['hours_left'])}h</td>"
                    f"<td>{apply_cell}</td>"
                    "</tr>"
                )

            subject = (
                f"🚨 YOU HAVEN'T APPLIED (T-{tier_hours}h): {len(included)} "
                f"drive{'s' if len(included) != 1 else ''} closing in"
            )
            body = (
                "<h2>Deadlines closing in — you haven't applied yet</h2>"
                '<table border="1" cellpadding="5" style="border-collapse: collapse;">'
                "<tr><th>Company</th><th>Role</th><th>Time Left</th><th>Apply</th></tr>"
                + "".join(rows) + "</table>"
            )
            if overflow:
                body += f"<p>+{overflow} more drive(s) also closing in this window.</p>"

            logger.info(
                "Sending batched %s escalation for %d drive(s)", alert_type, len(included)
            )
            if not self.notifier.send_email(subject=subject, body=body, is_html=True):
                logger.warning(
                    "Deadline escalation batch send failed for %s; not marking sent "
                    "so it retries next cycle", alert_type,
                )
                continue

            for item in included:
                self._mark_alert_sent(item["opp"]["id"], alert_type)

    def _should_send_alert(self, opp_id: int, alert_type: str) -> bool:
        """Check if this specific alert window was already triggered."""
        result = self.database.connection.execute(
            "SELECT 1 FROM sent_alerts WHERE opportunity_id = ? AND alert_type = ?",
            (opp_id, alert_type)
        ).fetchone()
        return result is None
        
    def _mark_alert_sent(self, opp_id: int, alert_type: str) -> None:
        """Record that an alert was sent to prevent duplicate spam."""
        self.database.connection.execute(
            "INSERT INTO sent_alerts (opportunity_id, alert_type, created_at) VALUES (?, ?, ?)",
            (opp_id, alert_type, utc_now_iso())
        )
        self.database.connection.commit()
        
    def _send_alert_email(
        self,
        opp: dict[str, Any],
        subject: str,
        alert_type: str,
        time_left: str,
    ) -> None:
        """Format and send the alert email using EmailNotifier."""
        body = f"""
        <h2>{subject}</h2>
        <table border="1" cellpadding="5" style="border-collapse: collapse;">
            <tr><th>Company</th><td>{opp.get('company_name')}</td></tr>
            <tr><th>Role</th><td>{opp.get('role')}</td></tr>
            <tr><th>Remaining Time</th><td>{time_left}</td></tr>
            <tr><th>Action Required</th><td>{opp.get('action_required', 'N/A')}</td></tr>
        </table>
        <br>
        <p>Please check your tracker or Gmail for more details.</p>
        """
        self.notifier.send_email(
            subject=subject,
            body=body,
            is_html=True
        )
