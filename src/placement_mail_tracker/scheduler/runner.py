"""Orchestration for Placement Mail Tracker sync cycles with advanced fault-tolerance safeguards."""

from __future__ import annotations

import logging
import socket
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from googleapiclient.errors import HttpError

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.gemini.extractor import GeminiExtractor
from placement_mail_tracker.gmail.filters import is_placement_mail
from placement_mail_tracker.gmail.gmail_client import GmailClient
from placement_mail_tracker.models.placement_record import PlacementRecord
from placement_mail_tracker.notifications.telegram import TelegramNotifier
from placement_mail_tracker.notifications.email_notifier import EmailNotifier
from placement_mail_tracker.sheets.client import SheetsClient
from placement_mail_tracker.utils.time import utc_now_iso
from placement_mail_tracker.utils.deduplication import find_best_match
from placement_mail_tracker.scheduler.digest_generator import DailyDigestGenerator

# ---------------------------------------------------------------------------
# Safeguard 1: Set a global network socket timeout to prevent infinite hangs
# ---------------------------------------------------------------------------
socket.setdefaulttimeout(15.0)

logger = logging.getLogger(__name__)


def map_extraction_to_opportunity(extraction: dict[str, Any]) -> dict[str, Any]:
    """Convert structured AI extraction dictionary to SQLite/Sheets opportunity fields."""
    opp = {
        "company_name": extraction.get("company_name"),
        "role": extraction.get("role"),
        "internship_or_fulltime": extraction.get("opportunity_type"),
        "package_or_stipend": extraction.get("package") or extraction.get("stipend"),
        "eligibility": extraction.get("eligibility"),
        "cgpa_requirement": extraction.get("cgpa_requirement"),
        "branches_allowed": extraction.get("eligible_branches"),
        "deadline": extraction.get("registration_deadline"),
        "interview_date": extraction.get("interview_date"),
        "oa_date": extraction.get("oa_date"),
        "registration_link": extraction.get("registration_link"),
        "work_location": extraction.get("location"),
        "hiring_process": extraction.get("hiring_process"),
        "important_notes": extraction.get("important_notes"),
    }
    return opp


# ---------------------------------------------------------------------------
# Safeguard 2: Notification deduplication to prevent student notification spam
# ---------------------------------------------------------------------------
def is_duplicate_notification(
    connection: sqlite3.Connection,
    opportunity_id: int,
    message: str,
) -> bool:
    """Check if an identical notification was recently successfully sent (within last 24h)."""
    try:
        row = connection.execute(
            """
            SELECT id FROM notifications
            WHERE opportunity_id = ?
              AND message = ?
              AND status = 'sent'
              AND created_at >= datetime('now', '-1 day')
            LIMIT 1;
            """,
            (opportunity_id, message),
        ).fetchone()
        return row is not None
    except sqlite3.Error as db_error:
        logger.warning("Could not query notification history for duplicates: %s", db_error)
        return False


@dataclass(slots=True)
class PlacementTrackerRunner:
    """Coordinate one Placement Mail Tracker sync cycle."""

    connection: sqlite3.Connection
    settings: Settings

    def run_once(self) -> None:
        """Run one sync cycle: fetch emails, filter, extract, store, sync, and notify."""
        logger.info("Initializing database manager and creating tables")
        database = DatabaseManager(connection=self.connection)
        database.create_tables()

        logger.info("Initializing API clients")
        gmail_client = GmailClient(self.settings)
        extractor = GeminiExtractor()
        sheets_client = SheetsClient(self.settings)
        notifier = TelegramNotifier(self.settings)
        digest_gen = DailyDigestGenerator(database, self.settings)

        # Trigger daily digest if it's past 8:00 AM local time
        if datetime.now().hour >= 8:
            try:
                digest_gen.generate_and_send()
            except Exception as e:
                logger.error("Failed to generate and send daily digest: %s", e)

        logger.info("Fetching recent messages from Gmail")
        messages: list[dict[str, Any]] = []

        # ---------------------------------------------------------------------------
        # Retry Mechanism: Fetch pending emails first (Tasks 4 & 5)
        # ---------------------------------------------------------------------------
        pending_records = self.connection.execute(
            "SELECT gmail_message_id FROM processed_emails WHERE processed_status = 'PENDING_EXTRACTION';"
        ).fetchall()

        pending_ids = [row[0] for row in pending_records]
        if pending_ids:
            logger.info(
                "Found %s pending emails in retry queue. Fetching them now.", len(pending_ids)
            )
            for pending_id in pending_ids:
                try:
                    msg = gmail_client.fetch_message(pending_id)
                    messages.append(msg)
                except Exception as e:
                    logger.warning("Could not fetch pending email %s: %s", pending_id, e)

        try:
            # Enforce rate-limiting retry logic on email fetching
            for attempt in range(1, 4):
                try:
                    messages = gmail_client.fetch_recent_messages(
                        max_results=self.settings.gmail_max_results
                    )
                    break
                except HttpError as api_error:
                    if api_error.resp.status in {429, 503} and attempt < 3:
                        sleep_time = attempt * 2.0
                        logger.warning(
                            "Gmail API rate limit hit (%s). Retrying in %ss...",
                            api_error.resp.status,
                            sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        raise
        except Exception as fetch_error:
            logger.error(
                "Could not fetch messages from Gmail API: %s. Halting sync cycle.", fetch_error
            )
            return

        logger.info("Fetched %s candidate messages", len(messages))

        processed_count = 0
        skipped_count = 0
        error_count = 0
        passed_filter_count = 0

        for msg in messages:
            msg_id = msg.get("message_id") or msg.get("id")
            if not msg_id:
                logger.warning("Email message is missing unique ID; skipping")
                continue

            # Check if this email was already processed successfully or legitimately skipped
            already_processed = self.connection.execute(
                "SELECT id FROM processed_emails WHERE gmail_message_id = ? AND processed_status IN ('processed', 'skipped') LIMIT 1;",
                (msg_id,),
            ).fetchone()

            if already_processed:
                # Log at INFO level (Task 7) to clarify why duplicate emails don't re-trigger pipeline actions
                logger.info(
                    "Email %s has already been processed in a previous cycle; skipping to avoid duplication",
                    msg_id,
                )
                continue

            subject = msg.get("subject", "(no subject)")
            sender = msg.get("sender", "")
            body = msg.get("body_text") or msg.get("body") or ""
            timestamp = msg.get("timestamp") or msg.get("received_at")
            thread_id = msg.get("thread_id")

            logger.info("Evaluating email relevance: Subject=%r, Sender=%r", subject, sender)
            decision = is_placement_mail(subject=subject, sender=sender, body=body)

            if not decision.is_placement:
                logger.info("Email %s is not relevant to placement/internship; skipping", msg_id)
                # Keep logs outside transaction so they are committed even if sync cycle interrupts
                database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    filter_score=decision.score,
                    filter_decision=asdict(decision),
                    processed_status="skipped",
                )
                skipped_count += 1
                continue

            passed_filter_count += 1
            # Task 4 pipeline logging
            logger.info("[INFO] Email %s passed filtering", msg_id)
            logger.info("[INFO] Starting Gemini extraction")

            try:
                extracted = extractor.extract(msg)
                if not extracted:
                    raise ValueError("Structured data extraction returned empty results")
                logger.info("[INFO] Gemini extraction successful")
                logger.info("EXTRACTED PAYLOAD: %s", extracted)

                # Handle model wrappers or dict directly
                extracted_dict = asdict(extracted) if not isinstance(extracted, dict) else extracted
                opp_data = map_extraction_to_opportunity(extracted_dict)

                # ---------------------------------------------------------------------------
                # Safeguard 3: Enforce strict SQLite transaction atomicity on per-email writes
                # ---------------------------------------------------------------------------
                with self.connection:
                    # Fuzzy deduplication pre-scan
                    active_opportunities = database.get_active_opportunities()
                    best_match = find_best_match(opp_data, active_opportunities)
                    if best_match and best_match.is_duplicate:
                        logger.info(
                            "Fuzzy duplicate detected for %s - %s (Confidence: %s%%)",
                            opp_data["company_name"],
                            opp_data["role"],
                            best_match.confidence_score,
                        )
                        # Align key fields with the duplicate to update instead of insert new
                        opp_data["company_name"] = best_match.candidate["company_name"]
                        opp_data["role"] = best_match.candidate["role"]

                    # Format Email Received Date to DD-MMM-YYYY HH:MM AM/PM
                    formatted_date = timestamp
                    try:
                        if timestamp:
                            dt = datetime.fromisoformat(timestamp)
                            formatted_date = dt.strftime("%d-%b-%Y %I:%M %p")
                    except Exception:
                        pass

                    opp_data["email_received_at"] = formatted_date
                    opp_data["last_update_timestamp"] = utc_now_iso()

                    # Insert or update in SQLite database
                    opp_id, created = database.insert_or_update_opportunity(
                        opp_data,
                        source_email_id=msg_id,
                        source_thread_id=thread_id,
                    )
                    action = "inserted" if created else "updated"
                    logger.info("[DB] Insert Success")
                    logger.info(
                        "[DB] Opportunity %s: %s - %s",
                        action,
                        opp_data["company_name"],
                        opp_data["role"],
                    )

                    # Send SMTP email notifications for critical updates
                    email_notifier = EmailNotifier(self.settings)
                    update_type = extracted_dict.get("update_type") or "new_opportunity"

                    record = PlacementRecord(
                        gmail_message_id=msg_id,
                        subject=subject,
                        sender=sender,
                        received_at=timestamp,
                        company_name=opp_data["company_name"],
                        role_title=opp_data["role"],
                        application_deadline=opp_data["deadline"],
                    )

                    # Notify only on: new opportunities, deadline updates, shortlists, interviews, and OAs
                    is_critical_update = created or update_type in {
                        "new_opportunity",
                        "deadline_update",
                        "shortlist",
                        "interview_update",
                        "oa_update",
                    }

                    if is_critical_update:
                        notification_msg = f"Email Alert [{update_type}]: {opp_data['company_name']} - {opp_data['role']}"
                        if not is_duplicate_notification(self.connection, opp_id, notification_msg):
                            logger.info(
                                "Sending SMTP email notification for critical update type: %s",
                                update_type,
                            )
                            success = email_notifier.send_opportunity_alert(
                                record, update_type=update_type
                            )
                            if success:
                                logger.info("[INFO] Notification email sent")
                                database.create_notification(
                                    opportunity_id=opp_id,
                                    channel="email",
                                    message=notification_msg,
                                    status="sent",
                                )
                        else:
                            logger.info(
                                "SMTP email alert was already sent recently; skipping notification"
                            )
                    else:
                        logger.info(
                            "Update type %r is not categorized as critical; skipping notification to avoid spam",
                            update_type,
                        )

                # Log processed email associated with opportunity (committed successfully)
                database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    opportunity_id=opp_id,
                    filter_score=decision.score,
                    filter_decision=asdict(decision),
                    processed_status="processed",
                )
                processed_count += 1

            except Exception as error:
                logger.exception("Failed to process email %s: %s", msg_id, error)
                # Keep logs outside transaction so they are committed even if opportunity fails
                # Mark as PENDING_EXTRACTION so it gets retried on the next cycle
                database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    filter_score=decision.score,
                    filter_decision=asdict(decision),
                    processed_status="PENDING_EXTRACTION",
                    error_message=str(error),
                )
                error_count += 1

        # ---------------------------------------------------------------------------
        # Task 6 & 8: Always synchronize all active records to ensure eventually consistent state
        # ---------------------------------------------------------------------------
        sheets_sync_successful = False
        logger.info("[SYNC] Starting Sheet Sync")
        try:
            # ---------------------------------------------------------------------------
            # Safeguard 4: Rate-limiting / Transient Sync Backoff loop for Sheets sync
            # ---------------------------------------------------------------------------
            for attempt in range(1, 4):
                try:
                    sheets_client.sync.sync_active_opportunities(database)
                    sheets_sync_successful = True
                    logger.info("[SYNC] Google Sheets Write Success")
                    break
                except HttpError as sheets_error:
                    if sheets_error.resp.status in {429, 503} and attempt < 3:
                        sleep_time = attempt * 2.0
                        logger.warning(
                            "Sheets API rate limit/server error hit (%s). Retrying in %ss...",
                            sheets_error.resp.status,
                            sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        raise
        except Exception as sync_error:
            # Google Sheets downtime does NOT crash or revert SQLite processed email state.
            # Auto-recovery catch-up syncs all active records next run.
            logger.error("[SYNC] Google Sheets Write Failed: %s", sync_error)

        # Task 7: Silent failures check and warning logging
        if len(messages) > 0:
            if passed_filter_count == 0:
                logger.warning("[WARNING] No placement emails passed filtering this cycle")
            if processed_count == 0:
                logger.warning("[WARNING] No database updates or inserts occurred this cycle")
            if not sheets_sync_successful:
                logger.warning("[WARNING] No rows synchronized to Google Sheets this cycle")
        else:
            logger.warning("[WARNING] No fetched emails found in this cycle")

        logger.info(
            "Sync cycle finished: processed=%s skipped=%s error=%s",
            processed_count,
            skipped_count,
            error_count,
        )


def run_once(connection: sqlite3.Connection, settings: Settings) -> None:
    """Run one full sync cycle."""
    PlacementTrackerRunner(connection=connection, settings=settings).run_once()
