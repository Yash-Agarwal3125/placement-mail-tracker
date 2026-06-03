"""Daily Digest generator for Placement Mail Tracker."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.notifications.email_notifier import EmailNotifier

logger = logging.getLogger(__name__)


class DailyDigestGenerator:
    """Generates and sends a daily summary of placement activities."""

    def __init__(self, database: DatabaseManager, settings: Settings):
        self.database = database
        self.settings = settings
        self.notifier = EmailNotifier(settings)

    def generate_and_send(self) -> bool:
        """Generate the digest and send it via email if it's the right time and there are updates."""
        now = datetime.now()
        yesterday_iso = (now - timedelta(days=1)).isoformat()
        
        # Check send time
        send_hour, send_minute = map(int, self.settings.digest_send_time.split(':'))
        if now.hour < send_hour or (now.hour == send_hour and now.minute < send_minute):
            logger.debug("Too early for daily digest. Waiting for %s", self.settings.digest_send_time)
            return False
            
        # Avoid duplicate digests on the same day
        if self._digest_already_sent_today(now):
            logger.info("Daily digest already sent today. Skipping.")
            return False
            
        opportunities = self.database.fetch_active_opportunities()
        
        new_opps: list[dict[str, Any]] = []
        status_changes: list[dict[str, Any]] = []
        upcoming_events: list[dict[str, Any]] = []
        deadlines: list[dict[str, Any]] = []
        action_required: list[dict[str, Any]] = []
        
        for opp in opportunities:
            # 1. New Opportunities
            created_at = opp.get("created_at", "")
            if created_at > yesterday_iso:
                new_opps.append(opp)
                
            # 2. Status Changes
            updated_at = opp.get("updated_at", "")
            if updated_at > yesterday_iso and created_at <= yesterday_iso:
                status_changes.append(opp)
                
            # 3. Upcoming Events
            event_date = opp.get("next_event_date")
            if event_date:
                upcoming_events.append(opp)
                
            # 4. Deadlines
            deadline = opp.get("deadline")
            if deadline:
                deadlines.append(opp)
                
            # 5. Action Required
            action = opp.get("action_required")
            if action and action.strip():
                action_required.append(opp)
                
        if not (new_opps or status_changes or upcoming_events or deadlines or action_required):
            logger.info("No significant updates for the daily digest.")
            # Mark as sent anyway so we don't keep evaluating today
            self._record_digest_sent()
            return False
            
        digest_body = self._format_digest(new_opps, status_changes, upcoming_events, deadlines, action_required)
        
        logger.info("Sending Daily Digest via email.")
        subject = f"Placement Daily Digest - {now.strftime('%d %b %Y')}"
        success = self.notifier.send_email(subject=subject, body=digest_body, is_html=True)
        
        if success:
            self._record_digest_sent()
            
        return success

    def _format_digest(
        self, 
        new_opps: list[dict[str, Any]], 
        status_changes: list[dict[str, Any]], 
        upcoming_events: list[dict[str, Any]],
        deadlines: list[dict[str, Any]],
        action_required: list[dict[str, Any]]
    ) -> str:
        lines = ["<h1>📊 Placement Mail Tracker - Daily Digest</h1>"]
        
        def format_opp(opp):
            return f"<b>{opp.get('company_name', 'Company')}</b> - {opp.get('role', 'Role')}"

        if new_opps:
            lines.append("<h2>🌟 NEW OPPORTUNITIES</h2><ul>")
            for opp in new_opps:
                lines.append(f"<li>{format_opp(opp)} (Priority: {opp.get('priority', 'MEDIUM')})</li>")
            lines.append("</ul>")
            
        if status_changes:
            lines.append("<h2>🔄 STATUS CHANGES</h2><ul>")
            for opp in status_changes:
                lines.append(f"<li>{format_opp(opp)} ➔ <code>{opp.get('current_status', 'UNKNOWN')}</code></li>")
            lines.append("</ul>")
            
        if upcoming_events:
            lines.append("<h2>📅 UPCOMING EVENTS</h2><ul>")
            for opp in upcoming_events:
                lines.append(f"<li>{format_opp(opp)}: {opp.get('next_event_date')}</li>")
            lines.append("</ul>")

        if deadlines:
            lines.append("<h2>⏰ DEADLINES</h2><ul>")
            for opp in deadlines:
                lines.append(f"<li>{format_opp(opp)}: {opp.get('deadline')}</li>")
            lines.append("</ul>")
            
        if action_required:
            lines.append("<h2>⚠️ ACTION REQUIRED</h2><ul>")
            for opp in action_required:
                lines.append(f"<li>{format_opp(opp)}: {opp.get('action_required')}</li>")
            lines.append("</ul>")
            
        lines.append("<hr><p><i>Generated automatically by Placement Mail Tracker</i></p>")
        return "".join(lines)
            
    def _digest_already_sent_today(self, now: datetime) -> bool:
        """Check the database to see if a digest was sent today."""
        today_str = now.strftime("%Y-%m-%d")
        row = self.database.connection.execute(
            "SELECT id FROM notifications WHERE channel = 'digest' AND status = 'sent' AND date(created_at) = ?",
            (today_str,)
        ).fetchone()
        return row is not None
        
    def _record_digest_sent(self) -> None:
        """Log the digest delivery in the notifications table."""
        self.database.create_notification(
            channel="digest",
            message="Daily digest execution",
            status="sent"
        )
