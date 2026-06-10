"""Orchestration for Placement Mail Tracker sync cycles with drive-centric architecture.

Phase 1: Drive-centric architecture (follow-up updates, not new rows).
Phase 2: Follow-up detection engine integrated into pipeline.
Phase 3: Rule-based extraction runs BEFORE Gemini to reduce API calls.
Phase 13: Email classification stored with each processed email.
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError

from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor as GeminiExtractor
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.eligibility import evaluate_eligibility
from placement_mail_tracker.extraction.rule_engine import (
    classify_email,
    detect_status_from_text,
    normalize_company_name,
)
from placement_mail_tracker.extraction.rule_engine import (
    extract_from_email as rule_extract,
)
from placement_mail_tracker.gmail.filters import is_placement_mail
from placement_mail_tracker.gmail.gmail_client import GmailClient
from placement_mail_tracker.reliability.status import RunReport, SyncMetrics
from placement_mail_tracker.sheets.client import SheetsClient
from placement_mail_tracker.utils.deduplication import find_best_match
from placement_mail_tracker.utils.time import utc_now_iso

# ---------------------------------------------------------------------------
# Safeguard 1: Set a global network socket timeout to prevent infinite hangs
# ---------------------------------------------------------------------------
socket.setdefaulttimeout(15.0)

logger = logging.getLogger(__name__)


def map_extraction_to_opportunity(extraction: dict[str, Any]) -> dict[str, Any]:
    """Convert structured AI extraction dictionary to SQLite/Sheets opportunity fields."""
    return {
        "company_name": extraction.get("company_name"),
        "role": extraction.get("role"),
        "internship_or_fulltime": extraction.get("opportunity_type") or extraction.get("category"),
        "package_or_stipend": (
            extraction.get("package") or extraction.get("stipend") or extraction.get("ctc")
        ),
        "eligibility": extraction.get("eligibility"),
        "cgpa_requirement": extraction.get("cgpa_requirement"),
        "branches_allowed": extraction.get("eligible_branches"),
        "deadline": extraction.get("registration_deadline") or extraction.get("deadline"),
        "interview_date": extraction.get("interview_date"),
        "oa_date": extraction.get("oa_date"),
        "registration_link": extraction.get("registration_link"),
        "work_location": extraction.get("location") or extraction.get("work_location"),
        "hiring_process": extraction.get("hiring_process"),
        "important_notes": extraction.get("important_notes"),
        "current_status": extraction.get("current_status"),
    }


# ---------------------------------------------------------------------------
# Safeguard 2: Notification deduplication
# ---------------------------------------------------------------------------
def is_duplicate_notification(
    connection: sqlite3.Connection,
    opportunity_id: int,
    message: str,
) -> bool:
    """Check if an identical notification was recently sent (within last 24h)."""
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
        logger.warning("Could not query notification history: %s", db_error)
        return False


@dataclass(slots=True)
class PlacementTrackerRunner:
    """Coordinate one Placement Mail Tracker sync cycle."""

    connection: sqlite3.Connection
    settings: Settings

    def run_once(self) -> RunReport:
        """Run one sync cycle: fetch, filter, extract (rule+AI), store, sync, notify."""
        report = RunReport(environment=self.settings.environment)

        try:
            logger.info("Initializing database manager and creating tables")
            database = DatabaseManager(connection=self.connection)
            database.create_tables()
        except Exception as database_error:
            logger.exception("Database initialization failed: %s", database_error)
            report.mark_component(
                "database",
                False,
                str(database_error),
                critical=True,
            )
            return report

        logger.info("Initializing API clients")
        gmail_client = GmailClient(self.settings)
        extractor = GeminiExtractor(self.settings)

        # Load user profile for eligibility filtering
        user_profile = UserProfile.load()
        sheets_client = SheetsClient(self.settings)

        # Daily digest (if configured)
        try:
            from placement_mail_tracker.scheduler.digest_generator import DailyDigestGenerator
            if datetime.now().hour >= 8:
                digest_gen = DailyDigestGenerator(database, self.settings)
                digest_gen.generate_and_send()
        except Exception as e:
            logger.debug("Daily digest skipped: %s", e)

        logger.info("Fetching recent messages from Gmail")
        messages: list[dict[str, Any]] = []

        # Retry pending emails first
        pending_records = self.connection.execute(
            """
            SELECT gmail_message_id, retry_count
            FROM processed_emails
            WHERE processed_status = 'PENDING_EXTRACTION';
            """
        ).fetchall()
        pending_ids = [row[0] for row in pending_records]
        pending_counts = {row[0]: row[1] for row in pending_records}
        if pending_ids:
            logger.info("Found %s pending emails in retry queue", len(pending_ids))
            for pending_id in pending_ids:
                try:
                    msg = gmail_client.fetch_message(pending_id)
                    messages.append(msg)
                except Exception as e:
                    logger.warning("Could not fetch pending email %s: %s", pending_id, e)

        # Read fetch_state.json
        fetch_state_path = Path(self.settings.fetch_state_file)
        if fetch_state_path.exists():
            try:
                state_data = json.loads(fetch_state_path.read_text(encoding="utf-8"))
                last_fetch_iso = state_data.get("last_successful_fetch")
                last_fetch_timestamp = int(datetime.fromisoformat(last_fetch_iso.replace("Z", "+00:00")).timestamp())
            except Exception:
                # Default to start of today if parsing fails
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                last_fetch_timestamp = int(today.timestamp())
        else:
            # Default to start of today
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            last_fetch_timestamp = int(today.timestamp())

        try:
            for attempt in range(1, 4):
                try:
                    messages = gmail_client.fetch_recent_messages_since(
                        timestamp_seconds=last_fetch_timestamp,
                        max_results=self.settings.gmail_max_results
                    )
                    break
                except HttpError as api_error:
                    if api_error.resp.status in {429, 503} and attempt < 3:
                        sleep_time = attempt * 2.0
                        logger.warning(
                            "Gmail API rate limit hit (%s). Retrying in %ss...",
                            api_error.resp.status, sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        raise
        except Exception as fetch_error:
            logger.error("Could not fetch messages from Gmail API: %s", fetch_error)
            report.mark_component(
                "gmail",
                False,
                str(fetch_error),
                critical=self.settings.is_production,
            )
            return report

        if gmail_client.last_error:
            report.mark_component(
                "gmail",
                False,
                gmail_client.last_error,
                critical=self.settings.is_production,
            )

        logger.info("Fetched %s candidate messages", len(messages))

        processed_count = 0
        skipped_count = 0
        error_count = 0
        gemini_calls = 0
        rule_only_count = 0
        drives_created = 0
        drives_updated = 0

        for msg in messages:
            msg_id = msg.get("message_id") or msg.get("id")
            if not msg_id:
                logger.warning("Email missing unique ID; skipping")
                continue

            already_processed = self.connection.execute(
                """
                SELECT id
                FROM processed_emails
                WHERE gmail_message_id = ?
                  AND processed_status IN ('processed', 'skipped', 'PERMANENT_FAILURE')
                LIMIT 1;
                """,
                (msg_id,),
            ).fetchone()

            if already_processed:
                logger.info("Email %s already processed; skipping", msg_id)
                continue

            subject = msg.get("subject", "(no subject)")
            sender = msg.get("sender", "")
            body = msg.get("body_text") or msg.get("body") or ""
            timestamp = msg.get("timestamp") or msg.get("received_at")
            thread_id = msg.get("thread_id")

            # Phase 13: Classify email
            classification = classify_email(subject, body)

            logger.info("Evaluating email: Subject=%r Classification=%s", subject, classification)
            decision = is_placement_mail(subject=subject, sender=sender, body=body)

            if not decision.is_placement:
                logger.info(
                    "Email %s not relevant; skipping (classification=%s)",
                    msg_id,
                    classification,
                )
                database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    filter_score=decision.score,
                    filter_decision=asdict(decision),
                    processed_status="skipped",
                    email_classification=classification,
                )
                skipped_count += 1
                continue

            try:
                # ======================================================
                # Phase 3: Rule-based extraction FIRST, Gemini fallback
                # ======================================================
                rule_result = rule_extract(subject, body, sender)
                logger.info(
                    "Rule extraction: confidence=%.0f%% needs_gemini=%s",
                    rule_result.confidence * 100, rule_result.needs_gemini,
                )

                if rule_result.needs_gemini:
                    # Fall back to Gemini for missing critical fields
                    logger.info("Rule extraction incomplete; calling Gemini")
                    try:
                        extracted = extractor.extract_from_email(msg)
                        if not extracted:
                            raise ValueError("Gemini extraction returned empty results")

                        extracted_dict = (
                            asdict(extracted) if not isinstance(extracted, dict) else extracted
                        )
                        opp_data = map_extraction_to_opportunity(extracted_dict)
                        gemini_calls += 1

                        # Merge: prefer Gemini data but keep rule-based status/classification
                        if rule_result.current_status != "OPEN":
                            opp_data["current_status"] = rule_result.current_status
                        if not opp_data.get("current_status"):
                            opp_data["current_status"] = rule_result.current_status
                    except Exception as gemini_err:
                        logger.warning("Gemini failed completely, falling back to rule engine: %s", gemini_err)
                        opp_data = rule_result.to_dict()
                else:
                    # Phase 3: Rule extraction sufficient - no Gemini call!
                    opp_data = rule_result.to_dict()
                    rule_only_count += 1
                    logger.info("Rule extraction sufficient; skipping Gemini (saved API call)")

                # Phase 4: Normalize company name
                if opp_data.get("company_name"):
                    opp_data["company_name"] = normalize_company_name(opp_data["company_name"])

                # Phase 2: Detect status from email text if not already set
                if not opp_data.get("current_status") or opp_data["current_status"] == "OPEN":
                    detected_status = detect_status_from_text(subject, body)
                    if detected_status != "OPEN":
                        opp_data["current_status"] = detected_status

                with self.connection:
                    # Fuzzy deduplication pre-scan
                    active_opportunities = database.get_active_opportunities()
                    best_match = find_best_match(opp_data, active_opportunities)
                    if best_match and best_match.is_duplicate:
                        logger.info(
                            "Fuzzy duplicate detected for %s - %s (Confidence: %s%%)",
                            opp_data["company_name"], opp_data["role"],
                            best_match.confidence_score,
                        )
                        opp_data["company_name"] = best_match.candidate["company_name"]
                        opp_data["role"] = best_match.candidate["role"]

                    # Format Email Received Date
                    formatted_date = timestamp
                    try:
                        if timestamp:
                            dt = datetime.fromisoformat(timestamp)
                            formatted_date = dt.strftime("%d-%b-%Y %I:%M %p")
                    except Exception:
                        pass

                    opp_data["email_received_at"] = formatted_date
                    opp_data["last_update_timestamp"] = utc_now_iso()

                    # Phase 5: Eligibility Filter
                    eligibility_status = evaluate_eligibility(opp_data, user_profile)
                    if eligibility_status != "MANUAL_REVIEW":
                        opp_data["eligibility_status"] = eligibility_status

                    # Feature 8: Priority Scoring
                    from placement_mail_tracker.utils.scoring import compute_priority
                    opp_data["priority"] = compute_priority(opp_data, user_profile)

                    # Phase 1: Insert or update drive (not create duplicate)
                    opp_id, created = database.insert_or_update_opportunity(
                        opp_data,
                        source_email_id=msg_id,
                        source_thread_id=thread_id,
                        email_classification=classification,
                    )
                    action = "inserted" if created else "updated"
                    if created:
                        drives_created += 1
                    else:
                        drives_updated += 1
                    logger.info(
                        "[DB] Drive %s: %s - %s (classification=%s, priority=%s)",
                        action,
                        opp_data["company_name"],
                        opp_data["role"],
                        classification,
                        opp_data["priority"],
                    )

                database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    opportunity_id=opp_id,
                    filter_score=decision.score,
                    filter_decision=asdict(decision),
                    processed_status="processed",
                    email_classification=classification,
                )
                processed_count += 1

            except Exception as error:
                logger.exception("Failed to process email %s: %s", msg_id, error)
                database.log_processed_email(
                    gmail_message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    received_at=timestamp,
                    filter_score=decision.score,
                    filter_decision=asdict(decision),
                    processed_status="PENDING_EXTRACTION",
                    error_message=str(error),
                    email_classification=classification,
                )
                error_count += 1

        # Sync to Google Sheets
        sheets_sync_successful = False
        logger.info("[SYNC] Starting Sheet Sync")
        try:
            for attempt in range(1, 4):
                try:
                    sheets_client.sync.sync_active_opportunities(database)
                    sheets_sync_successful = True
                    logger.info("[SYNC] Google Sheets Write Success")
                    break
                except HttpError as sheets_error:
                    if sheets_error.resp.status in {429, 503} and attempt < 3:
                        time.sleep(attempt * 2.0)
                    else:
                        raise
        except Exception as sync_error:
            logger.error("[SYNC] Google Sheets Write Failed: %s", sync_error)
            report.mark_component(
                "sheets",
                False,
                str(sync_error),
                critical=self.settings.is_production,
            )

        if not sheets_sync_successful and sheets_client.sync.last_error:
            report.mark_component(
                "sheets",
                False,
                sheets_client.sync.last_error,
                critical=self.settings.is_production,
            )

        # Feature 2 & 3: Smart Alerting
        logger.info("Checking for upcoming deadlines and events")
        try:
            from placement_mail_tracker.scheduler.alert_generator import AlertGenerator
            alert_generator = AlertGenerator(database, self.settings)
            alert_generator.check_and_send_alerts()
        except Exception as e:
            logger.exception("Alert generation failed: %s", e)
            report.mark_component(
                "notifications",
                False,
                str(e),
                critical=False,
            )

        # Summary
        gemini_savings = (
            f"{(rule_only_count / (rule_only_count + gemini_calls) * 100):.0f}%"
            if (rule_only_count + gemini_calls) > 0
            else "N/A"
        )

        logger.info(
            "Sync cycle finished: processed=%s skipped=%s error=%s "
            "gemini_calls=%s rule_only=%s gemini_savings=%s",
            processed_count, skipped_count, error_count,
            gemini_calls, rule_only_count, gemini_savings,
        )

        report.metrics = SyncMetrics(
            processed_messages=processed_count,
            skipped_messages=skipped_count,
            error_messages=error_count,
            drives_created=drives_created,
            drives_updated=drives_updated,
            gemini_calls=gemini_calls,
            rule_only=rule_only_count,
        )

        if error_count:
            report.add_warning(f"{error_count} email(s) failed processing")
        else:
            # If everything succeeded (no errors), update fetch state
            import json
            fetch_state_path = Path(self.settings.fetch_state_file)
            fetch_state_path.parent.mkdir(parents=True, exist_ok=True)
            fetch_state_path.write_text(
                json.dumps({"last_successful_fetch": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}),
                encoding="utf-8"
            )

        return report


def run_once(connection: sqlite3.Connection, settings: Settings) -> RunReport:
    """Run one full sync cycle."""
    return PlacementTrackerRunner(connection=connection, settings=settings).run_once()
