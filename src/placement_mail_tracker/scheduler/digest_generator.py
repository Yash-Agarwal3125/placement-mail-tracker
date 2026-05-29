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
        """Generate the digest and send it via email if there are updates."""
        now = datetime.now()
        yesterday_iso = (now - timedelta(days=1)).isoformat()
        
        # Avoid duplicate digests on the same day
        if self._digest_already_sent_today(now):
            logger.info("Daily digest already sent today. Skipping.")
            return False
            
        opportunities = self.database.get_active_opportunities()
        
        new_opps: list[dict[str, Any]] = []
        status_changes: list[dict[str, Any]] = []
        upcoming_actions: list[dict[str, Any]] = []
        
        for opp in opportunities:
            # 1. New Opportunities
            created_at = opp.get("created_at", "")
            if created_at > yesterday_iso:
                new_opps.append(opp)
                continue
                
            # 2. Status Changes
            updated_at = opp.get("updated_at", "")
            if updated_at > yesterday_iso:
                status_changes.append(opp)
                
            # 3. Action Required / Upcoming
            action = opp.get("action_required")
            if action and action.strip():
                upcoming_actions.append(opp)
                
        if not new_opps and not status_changes and not upcoming_actions:
            logger.info("No significant updates for the daily digest.")
            # Mark as sent anyway so we don't keep evaluating today
            self._record_digest_sent()
            return False
            
        digest_body = self._format_digest(new_opps, status_changes, upcoming_actions)
        
        logger.info("Sending Daily Digest via email.")
        success = self._send_email_digest(digest_body)
        
        if success:
            self._record_digest_sent()
            
        return success

    def _format_digest(self, new_opps: list[dict[str, Any]], status_changes: list[dict[str, Any]], actions: list[dict[str, Any]]) -> str:
        lines = ["# 📊 Placement Mail Tracker - Daily Digest\n"]
        
        if new_opps:
            lines.append("## 🌟 New Opportunities (Last 24h)")
            for opp in new_opps:
                role = opp.get("role", "Role")
                company = opp.get("company_name", "Company")
                lines.append(f"- **{company}** - {role}")
            lines.append("")
            
        if status_changes:
            lines.append("## 🔄 Status Updates (Last 24h)")
            for opp in status_changes:
                role = opp.get("role", "Role")
                company = opp.get("company_name", "Company")
                status = opp.get("current_status", "UNKNOWN")
                lines.append(f"- **{company}** - {role} ➔ `{status}`")
            lines.append("")
            
        if actions:
            lines.append("## ⚠️ Action Required / Upcoming")
            for opp in actions:
                role = opp.get("role", "Role")
                company = opp.get("company_name", "Company")
                action = opp.get("action_required", "")
                lines.append(f"- **{company}** - {role}: {action}")
            lines.append("")
            
        lines.append("---\n*Generated automatically by Placement Mail Tracker*")
        return "\n".join(lines)
        
    def _send_email_digest(self, body: str) -> bool:
        """Send the digest using the EmailNotifier's underlying server logic."""
        import smtplib
        from email.message import EmailMessage
        
        if not self.settings.smtp_host or not self.settings.smtp_password:
            logger.warning("SMTP not configured. Cannot send daily digest.")
            return False
            
        msg = EmailMessage()
        msg.set_content(body)
        msg["Subject"] = f"Placement Daily Digest - {datetime.now().strftime('%d %b %Y')}"
        msg["From"] = self.settings.smtp_username
        msg["To"] = self.settings.smtp_username  # Send to self
        
        try:
            with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port) as server:
                server.login(self.settings.smtp_username, self.settings.smtp_password.get_secret_value())
                server.send_message(msg)
            return True
        except Exception as error:
            logger.error("Failed to send daily digest email: %s", error)
            return False
            
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
