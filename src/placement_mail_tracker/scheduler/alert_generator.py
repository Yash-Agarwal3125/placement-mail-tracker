"""Smart alerting logic for upcoming deadlines and events."""

import logging
from datetime import datetime, timedelta
from typing import Any

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.notifications.email_notifier import EmailNotifier
from placement_mail_tracker.utils.time import parse_datetime_flexible

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
        """Feature 3: Event alerts at <48h, <24h, and <4h."""
        event_str = opp.get("next_event_date")
        if not event_str:
            return
            
        event_dt = parse_datetime_flexible(event_str)
        if not event_dt:
            return
            
        time_left = event_dt - now
        if time_left < timedelta(0):
            return
            
        hours_left = time_left.total_seconds() / 3600
        
        alert_type = None
        if hours_left <= 4:
            alert_type = "EVENT_4H"
        elif hours_left <= 24:
            alert_type = "EVENT_24H"
        elif hours_left <= 48:
            alert_type = "EVENT_48H"
            
        if alert_type and self._should_send_alert(opp["id"], alert_type):
            logger.info("Sending %s alert for %s", alert_type, opp["company_name"])
            self._send_alert_email(
                opp, 
                subject=f"📅 UPCOMING EVENT: {opp['company_name']} in <{int(hours_left)} hours",
                alert_type=alert_type,
                time_left=f"{int(hours_left)} hours"
            )
            self._mark_alert_sent(opp["id"], alert_type)
            
    def _should_send_alert(self, opp_id: int, alert_type: str) -> bool:
        """Check if this specific alert window was already triggered."""
        result = self.database.connection.execute(
            "SELECT 1 FROM sent_alerts WHERE opportunity_id = ? AND alert_type = ?",
            (opp_id, alert_type)
        ).fetchone()
        return result is None
        
    def _mark_alert_sent(self, opp_id: int, alert_type: str) -> None:
        """Record that an alert was sent to prevent duplicate spam."""
        from placement_mail_tracker.utils.time import utc_now_iso
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
