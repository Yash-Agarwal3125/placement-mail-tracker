"""Production-ready personal SMTP email notification service."""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage

from placement_mail_tracker.config.settings import Settings

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class EmailNotifier:
    """Send personal placement updates and alerts using Gmail SMTP."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.smtp_email = settings.smtp_email.strip()
        self.smtp_app_password = settings.smtp_app_password.strip()
        # Feature 7: Personal Email Delivery
        self.email_receiver = (
            settings.notification_email.strip() or settings.email_receiver.strip()
        )

    @property
    def is_configured(self) -> bool:
        """Check if all required SMTP settings are filled in .env."""
        return bool(self.smtp_email and self.smtp_app_password and self.email_receiver)

    def send_email(self, subject: str, body: str, is_html: bool = False) -> bool:
        """Generic method to send an email (used by Digest and Alerts)."""
        if not self.is_configured:
            logger.warning("SMTP email notifications are not configured; skipping email")
            return False

        message = EmailMessage()
        message["From"] = self.smtp_email
        message["To"] = self.email_receiver
        message["Subject"] = subject
        
        if is_html:
            message.set_content("Please enable HTML to view this email.")
            message.add_alternative(body, subtype="html")
        else:
            message.set_content(body)

        return self._send_smtp_message_with_retry(message, "email")

    def _send_smtp_message_with_retry(self, message: EmailMessage, subject_log: str) -> bool:
        backoffs = [2, 5, 10]
        for attempt, backoff in enumerate(backoffs + [0], 1):
            try:
                logger.info(
                    "Connecting to Gmail SMTP server for %s: %s",
                    subject_log, message["Subject"],
                )
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(self.smtp_email, self.smtp_app_password)
                    server.send_message(message)
                logger.info("SMTP email sent successfully to %s", self.email_receiver)
                return True
            except (
                smtplib.SMTPServerDisconnected, ssl.SSLEOFError,
                TimeoutError, ConnectionResetError,
            ) as error:
                if attempt <= len(backoffs):
                    logger.warning("[SMTP]\nRetry attempt %s/%s", attempt, len(backoffs))
                    time.sleep(backoff)
                else:
                    logger.error("Failed to deliver SMTP email after retries: %s", error)
                    return False
            except Exception as error:
                logger.error("Failed to deliver SMTP email: %s", error)
                return False
        return False
