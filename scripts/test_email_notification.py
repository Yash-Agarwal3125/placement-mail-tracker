"""Send a test email notification using Gmail SMTP.

This script uses a Gmail App Password, not your normal Gmail password.
Create one from your Google Account security settings, then add it to .env.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure simple console logging for this test script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )


def load_email_settings() -> tuple[str, str, str]:
    """Load SMTP email settings from the project .env file."""
    load_dotenv(PROJECT_ROOT / ".env")

    smtp_email = os.getenv("SMTP_EMAIL", "").strip()
    smtp_app_password = os.getenv("SMTP_APP_PASSWORD", "").strip()
    email_receiver = os.getenv("EMAIL_RECEIVER", "").strip()

    missing = [
        name
        for name, value in {
            "SMTP_EMAIL": smtp_email,
            "SMTP_APP_PASSWORD": smtp_app_password,
            "EMAIL_RECEIVER": email_receiver,
        }.items()
        if not value
    ]

    if missing:
        raise ValueError(f"Missing required .env variables: {', '.join(missing)}")

    return smtp_email, smtp_app_password, email_receiver


def build_message(sender: str, receiver: str) -> EmailMessage:
    """Create a formatted test notification email."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = receiver
    message["Subject"] = "Placement Mail Tracker - SMTP Test"

    message.set_content(
        """
Hello,

This is a test notification from Placement Mail Tracker.

If you received this email, Gmail SMTP authentication is working correctly.

Details:
- SMTP host: smtp.gmail.com
- TLS port: 587
- Sender configured through SMTP_EMAIL

Regards,
Placement Mail Tracker
""".strip()
    )

    return message


def send_email(sender: str, app_password: str, receiver: str) -> None:
    """Send one email using Gmail SMTP with TLS."""
    message = build_message(sender, receiver)

    logger.info("Connecting to Gmail SMTP server")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        logger.info("Logging in as %s", sender)
        server.login(sender, app_password)
        server.send_message(message)

    logger.info("Test email sent successfully to %s", receiver)


def main() -> int:
    """Run the SMTP email notification test."""
    setup_logging()

    try:
        sender, app_password, receiver = load_email_settings()
        send_email(sender, app_password, receiver)
    except ValueError as error:
        logger.error("%s", error)
        return 1
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. Check SMTP_EMAIL and Gmail App Password "
            "in your .env file."
        )
        return 1
    except smtplib.SMTPException as error:
        logger.error("SMTP error while sending email: %s", error)
        return 1
    except OSError as error:
        logger.error("Network error while connecting to Gmail SMTP: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
