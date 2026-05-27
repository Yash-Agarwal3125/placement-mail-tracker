"""Production-ready personal SMTP email notification service."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.models.placement_record import PlacementRecord

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class EmailNotifier:
    """Send personal placement updates and alerts using Gmail SMTP."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.smtp_email = settings.smtp_email.strip()
        self.smtp_app_password = settings.smtp_app_password.strip()
        self.email_receiver = settings.email_receiver.strip()

    @property
    def is_configured(self) -> bool:
        """Check if all required SMTP settings are filled in .env."""
        return bool(self.smtp_email and self.smtp_app_password and self.email_receiver)

    def send_opportunity_alert(
        self,
        record: PlacementRecord,
        *,
        update_type: str = "new_opportunity",
    ) -> bool:
        """Send a concise, beautifully formatted notification email for a placement update."""
        if not self.is_configured:
            logger.warning("SMTP email notifications are not configured; skipping alert")
            return False

        # Format update labels
        subject_label = "New Opportunity"
        if update_type == "deadline_update":
            subject_label = "Urgent: Deadline Extended"
        elif update_type == "shortlist":
            subject_label = "Result: Shortlist Released"
        elif update_type == "interview_update":
            subject_label = "Schedule: Interview Announced"
        elif update_type == "oa_update":
            subject_label = "Schedule: Online Assessment Announced"

        subject = f"[{subject_label}] {record.company_name} - {record.role_title}"

        body = f"""
Hello,

A placement update has been logged on your tracker:

🏢 Company Name: {record.company_name}
💼 Role Title:  {record.role_title}
📅 Application Deadline: {record.application_deadline or 'N/A'}
📧 Sender: {record.sender or 'Unknown'}
🔗 Source: {record.source_url or 'Gmail'}

To check the complete details or sync to Google Sheets, check your tracker dashboard.

Regards,
Placement Mail Tracker (Personal Automation)
""".strip()

        message = EmailMessage()
        message["From"] = self.smtp_email
        message["To"] = self.email_receiver
        message["Subject"] = subject
        message.set_content(body)

        try:
            logger.info("Connecting to Gmail SMTP server for alert: %s", subject)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.smtp_email, self.smtp_app_password)
                server.send_message(message)
            logger.info("SMTP notification sent successfully to %s", self.email_receiver)
            return True
        except Exception as error:
            logger.error("Failed to deliver SMTP email notification: %s", error)
            return False
