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
socket.setdefaulttimeout(60.0)

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

        database = self._init_database(report)
        if not database:
            return report

        logger.info("Initializing API clients")
        gmail_client = GmailClient(self.settings)
        extractor = GeminiExtractor(self.settings)
        sheets_client = SheetsClient(self.settings)
        user_profile = UserProfile.load()

        self._run_daily_digest(database)

        messages = self._fetch_messages(gmail_client, report)

        stats = {
            "processed": 0, "skipped": 0, "errors": 0, 
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0
        }
        if messages:
            stats = self._process_messages(messages, database, gmail_client, extractor, user_profile)
        
        self._execute_sync_pipelines(database, sheets_client, report)
        
        self._finalize_report(report, stats)
        return report

    def _init_database(self, report: RunReport) -> DatabaseManager | None:
        try:
            logger.info("Initializing database manager and creating tables")
            database = DatabaseManager(connection=self.connection)
            database.create_tables()

            purged = database.purge_old_processed_emails(30)
            if isinstance(purged, int) and purged > 0:
                logger.info("Purged %d old processed emails from DB", purged)
            return database
        except Exception as database_error:
            logger.exception("Database initialization failed: %s", database_error)
            report.mark_component("database", False, str(database_error), critical=True)
            return None

    def _run_daily_digest(self, database: DatabaseManager) -> None:
        try:
            from placement_mail_tracker.scheduler.digest_generator import DailyDigestGenerator

            if datetime.now().hour >= 8:
                digest_gen = DailyDigestGenerator(database, self.settings)
                digest_gen.generate_and_send()
        except Exception as e:
            logger.debug("Daily digest skipped: %s", e)

    def _fetch_messages(self, gmail_client: GmailClient, report: RunReport) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        pending_records = self.connection.execute(
            """
            SELECT gmail_message_id, retry_count
            FROM processed_emails
            WHERE processed_status = 'PENDING_EXTRACTION';
            """
        ).fetchall()

        pending_ids = [row[0] for row in pending_records]
        if pending_ids:
            logger.info("Found %s pending emails in retry queue", len(pending_ids))
            for pending_id in pending_ids:
                try:
                    msg = gmail_client.fetch_message(pending_id)
                    messages.append(msg)
                except Exception as e:
                    logger.warning("Could not fetch pending email %s: %s", pending_id, e)

        fetch_state_path = Path(self.settings.fetch_state_file)
        if fetch_state_path.exists():
            try:
                state_data = json.loads(fetch_state_path.read_text(encoding="utf-8"))
                last_fetch_iso = state_data.get("last_successful_fetch")
                last_fetch_timestamp = int(
                    datetime.fromisoformat(last_fetch_iso.replace("Z", "+00:00")).timestamp()
                )
            except Exception:
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                last_fetch_timestamp = int(today.timestamp())
        else:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            last_fetch_timestamp = int(today.timestamp())

        try:
            for attempt in range(1, 4):
                try:
                    recent = gmail_client.fetch_recent_messages_since(
                        timestamp_seconds=last_fetch_timestamp,
                        max_results=self.settings.gmail_max_results,
                    )
                    messages.extend(recent)
                    break
                except HttpError as api_error:
                    if api_error.resp.status in {429, 503} and attempt < 3:
                        sleep_time = attempt * 2.0
                        logger.warning(
                            "Retry attempt %s. Backoff duration %ss. Exception type: HttpError (%s)",
                            attempt,
                            sleep_time,
                            api_error.resp.status,
                        )
                        time.sleep(sleep_time)
                    else:
                        raise
        except Exception as fetch_error:
            logger.error("Could not fetch messages from Gmail API: %s", fetch_error)
            report.mark_component(
                "gmail", False, str(fetch_error), critical=self.settings.is_production
            )
            return []

        logger.info("Fetched %s candidate messages", len(messages))
        return messages

    def _process_messages(
        self,
        messages: list[dict[str, Any]],
        database: DatabaseManager,
        gmail_client: GmailClient,
        extractor: GeminiExtractor,
        user_profile: UserProfile,
    ) -> dict[str, int]:
        stats = {
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "gemini_calls": 0,
            "rule_only": 0,
            "created": 0,
            "updated": 0,
        }

        for msg in messages:
            self._process_single_message(msg, database, extractor, user_profile, stats)

        return stats

    def _process_single_message(
        self,
        msg: dict[str, Any],
        database: DatabaseManager,
        extractor: GeminiExtractor,
        user_profile: UserProfile,
        stats: dict[str, int],
    ) -> None:
        msg_id = msg.get("message_id") or msg.get("id")
        if not msg_id:
            logger.warning("Email missing unique ID; skipping")
            return

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
            return

        subject = msg.get("subject", "(no subject)")
        sender = msg.get("sender", "")
        body = msg.get("body_text") or msg.get("body") or ""
        timestamp = msg.get("timestamp") or msg.get("received_at")
        thread_id = msg.get("thread_id")

        classification = classify_email(subject, body)
        logger.info("Evaluating email: Subject=%r Classification=%s", subject, classification)
        decision = is_placement_mail(subject=subject, sender=sender, body=body)

        if not decision.is_placement:
            logger.info(
                "Email %s not relevant; skipping (classification=%s)", msg_id, classification
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
            stats["skipped"] += 1
            return

        try:
            rule_result = rule_extract(subject, body, sender)
            logger.info(
                "Rule extraction: confidence=%.0f%% needs_gemini=%s",
                rule_result.confidence * 100,
                rule_result.needs_gemini,
            )

            if rule_result.needs_gemini:
                logger.info("Rule extraction incomplete; calling Gemini")
                try:
                    extracted = extractor.extract_from_email(msg)
                    if not extracted:
                        raise ValueError("Gemini extraction returned empty results")
                    extracted_dict = (
                        asdict(extracted) if not isinstance(extracted, dict) else extracted
                    )
                    opp_data = map_extraction_to_opportunity(extracted_dict)
                    stats["gemini_calls"] += 1

                    if rule_result.current_status != "OPEN":
                        opp_data["current_status"] = rule_result.current_status
                    if not opp_data.get("current_status"):
                        opp_data["current_status"] = rule_result.current_status
                except Exception as gemini_err:
                    logger.warning(
                        "Gemini failed completely, falling back to rule engine: %s", gemini_err
                    )
                    opp_data = rule_result.to_dict()
            else:
                opp_data = rule_result.to_dict()
                stats["rule_only"] += 1
                logger.info("Rule extraction sufficient; skipping Gemini (saved API call)")

            if opp_data.get("company_name"):
                opp_data["company_name"] = normalize_company_name(opp_data["company_name"])

            if not opp_data.get("current_status") or opp_data["current_status"] == "OPEN":
                detected_status = detect_status_from_text(subject, body)
                if detected_status != "OPEN":
                    opp_data["current_status"] = detected_status

            with self.connection:
                active_opportunities = database.get_active_opportunities()
                best_match = find_best_match(opp_data, active_opportunities)
                if best_match and best_match.is_duplicate:
                    logger.info(
                        "Fuzzy duplicate detected for %s - %s (Confidence: %s%%)",
                        opp_data["company_name"],
                        opp_data["role"],
                        best_match.confidence_score,
                    )
                    opp_data["company_name"] = best_match.candidate["company_name"]
                    opp_data["role"] = best_match.candidate["role"]

                formatted_date = timestamp
                try:
                    if timestamp:
                        if "T" in timestamp:
                            dt = datetime.fromisoformat(timestamp)
                        else:
                            dt = datetime.strptime(timestamp, "%d-%b-%Y %I:%M %p")
                        formatted_date = dt.strftime("%d-%b-%Y %I:%M %p")
                except Exception:
                    pass

                opp_data["email_received_at"] = formatted_date
                opp_data["last_update_timestamp"] = utc_now_iso()

                eligibility_status = evaluate_eligibility(opp_data, user_profile)
                if eligibility_status != "MANUAL_REVIEW":
                    opp_data["eligibility_status"] = eligibility_status

                from placement_mail_tracker.utils.scoring import compute_priority

                opp_data["priority"] = compute_priority(opp_data, user_profile)

                opp_id, created = database.insert_or_update_opportunity(
                    opp_data,
                    source_email_id=msg_id,
                    source_thread_id=thread_id,
                    email_classification=classification,
                )
                action = "inserted" if created else "updated"
                if created:
                    stats["created"] += 1
                else:
                    stats["updated"] += 1
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
            stats["processed"] += 1

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
            stats["errors"] += 1

    def _execute_sync_pipelines(
        self, database: DatabaseManager, sheets_client: SheetsClient, report: RunReport
    ) -> None:
        sheets_sync_successful = False
        logger.info("[SYNC] Starting Sheet Sync")
        try:
            sheets_client.sync.sync_active_opportunities(database)
            sheets_sync_successful = True
            logger.info("[SYNC] Google Sheets Write Success")
        except Exception as e:
            logger.exception("[SYNC] Google Sheets Sync Failed: %s", e)

        if not sheets_sync_successful:
            report.mark_component(
                "google_sheets",
                False,
                sheets_client.sync.last_error,
                critical=self.settings.is_production,
            )

        logger.info("Checking for upcoming deadlines and events")
        try:
            from placement_mail_tracker.scheduler.alert_generator import AlertGenerator

            alert_generator = AlertGenerator(database, self.settings)
            alert_generator.check_and_send_alerts()
        except Exception as e:
            logger.exception("Alert generation failed: %s", e)
            report.mark_component("notifications", False, str(e), critical=False)

    def _finalize_report(self, report: RunReport, stats: dict[str, int]) -> None:
        gemini_savings = (
            f"{(stats['rule_only'] / (stats['rule_only'] + stats['gemini_calls']) * 100):.0f}%"
            if (stats["rule_only"] + stats["gemini_calls"]) > 0
            else "N/A"
        )

        logger.info(
            "Sync cycle finished: processed=%s skipped=%s error=%s "
            "gemini_calls=%s rule_only=%s gemini_savings=%s",
            stats["processed"],
            stats["skipped"],
            stats["errors"],
            stats["gemini_calls"],
            stats["rule_only"],
            gemini_savings,
        )

        report.metrics = SyncMetrics(
            processed_messages=stats["processed"],
            skipped_messages=stats["skipped"],
            error_messages=stats["errors"],
            drives_created=stats["created"],
            drives_updated=stats["updated"],
            gemini_calls=stats["gemini_calls"],
            rule_only=stats["rule_only"],
        )

        if stats["errors"]:
            report.add_warning(f"{stats['errors']} email(s) failed processing")

        import json

        fetch_state_path = Path(self.settings.fetch_state_file)
        fetch_state_path.parent.mkdir(parents=True, exist_ok=True)
        fetch_state_path.write_text(
            json.dumps({"last_successful_fetch": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}),
            encoding="utf-8",
        )
