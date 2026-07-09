"""Orchestration for Placement Mail Tracker sync cycles with drive-centric architecture."""

from __future__ import annotations

import json
import logging
import re
import socket
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError

from placement_mail_tracker.ai.attachments import extract_attachment_text, is_image_attachment
from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor as GeminiExtractor
from placement_mail_tracker.ai.gemini_extractor import GeminiQuotaExhaustedError
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
from placement_mail_tracker.extraction.validation import validate_opportunity_data
from placement_mail_tracker.gmail.filters import is_placement_mail
from placement_mail_tracker.gmail.gmail_client import GmailClient
from placement_mail_tracker.reliability.status import RunReport, SyncMetrics
from placement_mail_tracker.sheets.client import SheetsClient
from placement_mail_tracker.utils.deduplication import find_best_match
from placement_mail_tracker.utils.scoring import compute_priority
from placement_mail_tracker.utils.time import parse_datetime_flexible, utc_now_iso

# ---------------------------------------------------------------------------
# Safeguard 1: Set a global network socket timeout to prevent infinite hangs
# ---------------------------------------------------------------------------
socket.setdefaulttimeout(60.0)

logger = logging.getLogger(__name__)

_LPA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lpa|lakh(?:s)?|lac(?:s)?)", re.IGNORECASE)


def _parse_lpa(package_str: str | None) -> float | None:
    if not package_str:
        return None
    m = _LPA_RE.search(str(package_str))
    return float(m.group(1)) if m else None


def _classify_dream(lpa: float | None) -> str:
    if lpa is None:
        return "NORMAL"
    if lpa >= 40:
        return "SUPER_DREAM"
    if lpa >= 20:
        return "DREAM"
    return "NORMAL"


# Classifications that represent a status update to an existing drive rather
# than a brand-new opportunity. Used to decide when Gemini can be skipped.
_FOLLOWUP_CLASSIFICATIONS = frozenset(
    {"OA_UPDATE", "INTERVIEW_UPDATE", "SHORTLIST_UPDATE", "OFFER_UPDATE", "DRIVE_UPDATE"}
)

# Advancement-only statuses that should only be trusted for known-thread follow-ups.
# Mass announcements can trigger these keywords on brand-new drives; gate them here.
_ADVANCEMENT_STATUSES = frozenset({"SHORTLISTED", "SELECTED", "OFFER_RECEIVED", "HR"})

# Placeholder company values that mean "extraction failed", not a real drive.
_UNIDENTIFIED_COMPANIES = frozenset({"", "unknown", "unknown company"})

# Extraction failures are retried at most this many times total (FS INV-26 / BR-16).
# On the RETRY_MAX-th failure the email moves to PERMANENT_FAILURE (dead-letter).
_RETRY_MAX = 3

# Gemini occasionally emits a status vocabulary that differs from the rest of
# the system (which keys everything off OPEN/REGISTERED/...). Map the strays to
# the canonical values so such drives still appear in the Active sheet and get
# the right action_required.
_STATUS_CANONICAL = {
    "NEW": "OPEN",
    "PPT": "OPEN",
    "PRE_PLACEMENT_TALK": "OPEN",
    "APPLIED": "REGISTERED",
}


def _warn_data_quality(opp_data: dict[str, Any], msg_id: str) -> None:
    """Log warnings for common data quality issues; never blocks processing."""
    company = opp_data.get("company_name") or ""
    if not opp_data.get("package_or_stipend"):
        logger.warning("[DQ] %s (%s): missing stipend/package", company, msg_id)
    if not opp_data.get("eligibility") and not opp_data.get("branches_allowed"):
        logger.warning("[DQ] %s (%s): missing eligibility info", company, msg_id)
    deadline = opp_data.get("deadline")
    if deadline:
        if parse_datetime_flexible(str(deadline)) is None:
            logger.warning("[DQ] %s (%s): unparseable deadline %r", company, msg_id, deadline)


def _is_identifiable_company(name: str | None) -> bool:
    """Return True when ``name`` is a real company (not blank/Unknown)."""
    return bool(name) and name.strip().casefold() not in _UNIDENTIFIED_COMPANIES


def canonicalize_status(status: str | None) -> str | None:
    """Map model status synonyms (NEW, PPT, ...) onto the system vocabulary."""
    if not status:
        return status
    upper = status.strip().upper()
    return _STATUS_CANONICAL.get(upper, upper)


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
        "current_status": canonicalize_status(extraction.get("current_status")),
        "degree_level": extraction.get("degree_level") or "UNKNOWN",
        # Transient: the Gemini model's self-reported confidence, consumed only
        # by validate_opportunity_data() below. Not an opportunities column —
        # DatabaseManager._normalize_opportunity only reads known field names,
        # so an extra "confidence" key here is dropped harmlessly at storage.
        "confidence": extraction.get("confidence"),
    }


def _derive_next_event_date(opportunity: dict[str, Any]) -> str | None:
    """Return the soonest scheduled event (OA or interview) as a string.

    Picks the earliest of ``oa_date``/``interview_date`` that is today or in the
    future; falls back to the earliest parseable one if all are in the past.
    Returns ``None`` when neither date is present/parseable so an existing value
    is preserved. Dates stay as their original strings for display; downstream
    consumers re-parse with :func:`parse_datetime_flexible`.
    """
    candidates: list[tuple[datetime, str]] = []
    for field_name in ("oa_date", "interview_date"):
        raw = opportunity.get(field_name)
        if not raw:
            continue
        parsed = parse_datetime_flexible(str(raw))
        if parsed:
            candidates.append((parsed, str(raw)))

    if not candidates:
        return None

    now = datetime.now()
    upcoming = sorted((c for c in candidates if c[0] >= now), key=lambda c: c[0])
    if upcoming:
        return upcoming[0][1]
    # All events are in the past – keep the most recent one for reference.
    return sorted(candidates, key=lambda c: c[0], reverse=True)[0][1]


@dataclass(slots=True)
class PlacementTrackerRunner:
    """Coordinate one Placement Mail Tracker sync cycle."""

    connection: sqlite3.Connection
    settings: Settings

    def run_once(
        self, *, calendar_dry_run: bool = False, calendar_rebuild: bool = False
    ) -> RunReport:
        """Run one sync cycle: fetch, filter, extract (rule+AI), store, sync, notify."""
        run_start = datetime.now()
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

        # Capture the fetch-window boundary BEFORE fetching. Any email that
        # arrives after this instant is picked up on the next run, and the
        # window is only advanced if the fetch actually succeeds (see
        # _finalize_report) so a transient Gmail outage never drops mail.
        fetch_started_at = utc_now_iso()
        messages = self._fetch_messages(gmail_client, report)

        stats = {
            "processed": 0, "skipped": 0, "errors": 0,
            "gemini_calls": 0, "rule_only": 0, "created": 0, "updated": 0
        }
        if messages:
            stats = self._process_messages(
                messages, database, gmail_client, extractor, user_profile
            )

        self._execute_sync_pipelines(
            database,
            sheets_client,
            report,
            run_start,
            calendar_dry_run=calendar_dry_run,
            calendar_rebuild=calendar_rebuild,
        )

        self._finalize_report(report, stats, fetch_started_at)
        return report

    def _init_database(self, report: RunReport) -> DatabaseManager | None:
        try:
            logger.info("Initializing database manager and creating tables")
            # DatabaseManager.__init__ already creates tables; no second call needed.
            database = DatabaseManager(connection=self.connection)

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
                            "Retry attempt %s. Backoff %ss. Exception: HttpError (%s)",
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

        # In non-production env, GmailClient._search swallows HttpError/auth
        # failures and returns [] (so a personal run never crashes). That empty
        # list must NOT be mistaken for a successful zero-mail fetch: if it were,
        # _finalize_report would advance the fetch window past mail we never read
        # (FS INV-7/INV-8). last_error is typed str | None, so a genuine suppressed
        # failure is always a non-empty string; a healthy fetch resets it to None.
        if isinstance(gmail_client.last_error, str) and gmail_client.last_error.strip():
            logger.error(
                "Gmail fetch reported a suppressed error (%s env); not advancing "
                "fetch window: %s",
                self.settings.environment,
                gmail_client.last_error,
            )
            report.mark_component(
                "gmail",
                False,
                gmail_client.last_error,
                critical=self.settings.is_production,
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
            self._process_single_message(
                msg, database, extractor, user_profile, stats, gmail_client=gmail_client
            )

        return stats

    def _process_single_message(
        self,
        msg: dict[str, Any],
        database: DatabaseManager,
        extractor: GeminiExtractor,
        user_profile: UserProfile,
        stats: dict[str, int],
        gmail_client: GmailClient | None = None,
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

            # Phase 6 cost guard: a follow-up on a thread we already track does
            # not need Gemini. The existing drive already carries company/role,
            # and rule-based status/date detection is enough to update it. This
            # spares a paid Gemini call for the common "OA scheduled /
            # shortlisted / interview" follow-ups that arrive in-thread.
            existing_thread_drive = (
                database.fetch_opportunity_by_thread_id(thread_id) if thread_id else None
            )
            known_thread_followup = bool(
                existing_thread_drive is not None
                and (
                    rule_result.current_status != "OPEN"
                    or rule_result.email_classification in _FOLLOWUP_CLASSIFICATIONS
                )
            )
            # oa_date/interview_date have no rule-based extraction path at all,
            # so the cost guard's premise ("rule-based date detection is
            # enough") does not hold for the two classifications whose entire
            # purpose is announcing one of those dates — never skip Gemini
            # for those, even on a known thread.
            date_announcement = rule_result.email_classification in (
                "OA_UPDATE", "INTERVIEW_UPDATE"
            )

            if rule_result.needs_gemini and (not known_thread_followup or date_announcement):
                logger.info("Rule extraction incomplete; calling Gemini")
                attachment_text, image_parts = self._prepare_attachments(gmail_client, msg)
                try:
                    extracted = extractor.extract_from_email(
                        msg, attachment_text=attachment_text, image_parts=image_parts
                    )
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
                except GeminiQuotaExhaustedError:
                    # Daily quota is genuinely exhausted for every model we
                    # were allowed to try. Don't silently degrade to the
                    # rule-only extraction and mark this mail "processed" —
                    # propagate so the outer handler defers it (PENDING_
                    # EXTRACTION) for a later run once quota resets.
                    raise
                except Exception as gemini_err:
                    logger.warning(
                        "Gemini failed completely, falling back to rule engine: %s", gemini_err
                    )
                    opp_data = rule_result.to_dict()
            else:
                opp_data = rule_result.to_dict()
                stats["rule_only"] += 1
                # Preserve identity fields from the tracked drive so a rule-only
                # follow-up never overwrites a real company/role with a blank.
                if known_thread_followup and existing_thread_drive is not None:
                    if not opp_data.get("company_name"):
                        opp_data["company_name"] = existing_thread_drive["company_name"]
                    if not opp_data.get("role"):
                        opp_data["role"] = existing_thread_drive["role"]
                    logger.info(
                        "Known-thread follow-up; updating via rules, skipped Gemini"
                    )
                else:
                    logger.info("Rule extraction sufficient; skipping Gemini (saved API call)")

            # Merge degree_level: prefer rule result over UNKNOWN from Gemini.
            if (
                opp_data.get("degree_level") in (None, "UNKNOWN")
                and rule_result.degree_level != "UNKNOWN"
            ):
                opp_data["degree_level"] = rule_result.degree_level
            opp_data["dream_category"] = _classify_dream(
                _parse_lpa(opp_data.get("package_or_stipend"))
            )

            if opp_data.get("company_name"):
                opp_data["company_name"] = normalize_company_name(opp_data["company_name"])

            # Don't create brand-new drives we can't even attribute to a company.
            # Such rows (company "Unknown" + garbage role) pollute the DB/sheet and
            # have previously fired useless deadline alerts. Follow-ups on a known
            # thread are exempt because their identity comes from the tracked drive.
            if not known_thread_followup and not _is_identifiable_company(
                opp_data.get("company_name")
            ):
                logger.info(
                    "Email %s has no identifiable company; not creating a drive", msg_id
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

            if not opp_data.get("current_status") or opp_data["current_status"] == "OPEN":
                detected_status = detect_status_from_text(subject, body)
                if detected_status != "OPEN":
                    opp_data["current_status"] = detected_status

            # Gate advancement-only statuses behind known-thread follow-ups.
            # Mass announcements ("congratulations to all selected students") contain
            # keywords that trigger SHORTLISTED/SELECTED/OFFER_RECEIVED on new drives.
            # Only trust these statuses when we are already tracking this specific thread.
            if not known_thread_followup and (
                opp_data.get("current_status") or "OPEN"
            ).upper() in _ADVANCEMENT_STATUSES:
                opp_data["current_status"] = "OPEN"

            _warn_data_quality(opp_data, msg_id)

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

                # Derive the next upcoming event so deadline/event alerts, the
                # daily digest, priority scoring and the sheet all have a value
                # to work with (these features were previously dormant).
                next_event = _derive_next_event_date(opp_data)
                if next_event:
                    opp_data["next_event_date"] = next_event

                opp_data["priority"] = compute_priority(opp_data, user_profile)

                # Validate against the *effective* dates, not just what this
                # round's extraction restated. Follow-ups on a known drive
                # frequently omit deadline/oa_date/interview_date entirely
                # (COALESCE at the DB layer then preserves the previously
                # stored value — see _update_opportunity_row) while opp_data
                # itself only has what this round found. Validating on
                # opp_data alone would reset validation_flags to "looks fine"
                # every time a follow-up doesn't restate an already-flagged
                # date, even though the implausible value is still sitting in
                # the row untouched. Fall back to the tracked/matched drive's
                # stored values for this check only (never for storage).
                existing_for_validation = existing_thread_drive or (
                    best_match.candidate if best_match and best_match.is_duplicate else None
                )
                effective_dates = dict(opp_data)
                if existing_for_validation is not None:
                    # existing_thread_drive is a sqlite3.Row (no .get()); dict()
                    # normalizes both possible sources to a plain mapping.
                    existing_for_validation = dict(existing_for_validation)
                    for date_field in ("deadline", "oa_date", "interview_date"):
                        if not effective_dates.get(date_field):
                            effective_dates[date_field] = existing_for_validation.get(date_field)
                opp_data["validation_flags"] = validate_opportunity_data(
                    effective_dates, email_received_at=timestamp
                )

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

        except GeminiQuotaExhaustedError as quota_error:
            # Quota exhaustion is a today-only operational condition, not a
            # defect in this email — don't burn down the PERMANENT_FAILURE
            # retry budget (_RETRY_MAX) meant for genuinely broken emails.
            # Leave retry_count untouched so the email waits in the
            # PENDING_EXTRACTION retry queue (picked back up by
            # _fetch_messages) until quota frees up, bounded only by the
            # existing 30-day purge_old_processed_emails TTL.
            logger.warning(
                "Deferring email %s: Gemini quota exhausted for all attempted "
                "models: %s", msg_id, quota_error,
            )
            row = self.connection.execute(
                "SELECT retry_count FROM processed_emails WHERE gmail_message_id = ?",
                (msg_id,),
            ).fetchone()
            database.log_processed_email(
                gmail_message_id=msg_id,
                subject=subject,
                sender=sender,
                received_at=timestamp,
                filter_score=decision.score,
                filter_decision=asdict(decision),
                processed_status="PENDING_EXTRACTION",
                error_message=str(quota_error),
                email_classification=classification,
                retry_count=row["retry_count"] if row else 0,
                last_retry_at=utc_now_iso(),
            )
            stats["errors"] += 1

        except Exception as error:
            logger.exception("Failed to process email %s: %s", msg_id, error)
            row = self.connection.execute(
                "SELECT retry_count FROM processed_emails WHERE gmail_message_id = ?",
                (msg_id,),
            ).fetchone()
            new_count = (row["retry_count"] if row else 0) + 1
            if new_count >= _RETRY_MAX:
                next_status = "PERMANENT_FAILURE"
                logger.error(
                    "Email %s exhausted %d retries; moving to PERMANENT_FAILURE (dead-letter)",
                    msg_id,
                    _RETRY_MAX,
                )
            else:
                next_status = "PENDING_EXTRACTION"
            database.log_processed_email(
                gmail_message_id=msg_id,
                subject=subject,
                sender=sender,
                received_at=timestamp,
                filter_score=decision.score,
                filter_decision=asdict(decision),
                processed_status=next_status,
                error_message=str(error),
                email_classification=classification,
                retry_count=new_count,
                last_retry_at=utc_now_iso(),
            )
            stats["errors"] += 1

    def _prepare_attachments(
        self, gmail_client: GmailClient | None, msg: dict[str, Any]
    ) -> tuple[str, list[tuple[bytes, str]]]:
        """Fetch and classify this mail's attachments for the Gemini call.

        Only invoked right before a Gemini call that is already happening
        (see the call site in ``_process_single_message``) — never adds a
        Gmail or Gemini call for mails Gemini would not otherwise touch.
        Returns (attachment_text, image_parts): pre-extracted .xlsx/.pdf text
        to append to the prompt, and raw (bytes, mime_type) pairs for
        image attachments to route to Gemini multimodal separately.
        """
        attachments = msg.get("attachments") or []
        if not attachments or gmail_client is None:
            return "", []

        msg_id = msg.get("message_id") or msg.get("id") or ""
        text_chunks: list[str] = []
        image_parts: list[tuple[bytes, str]] = []

        for attachment in attachments:
            filename = attachment.get("filename", "")
            mime_type = attachment.get("mimeType", "")
            attachment_id = attachment.get("attachmentId")
            if not attachment_id:
                continue
            try:
                data = gmail_client.fetch_attachment_bytes(msg_id, attachment_id)
            except Exception as error:
                logger.warning(
                    "Could not fetch attachment %r for %s: %s", filename, msg_id, error
                )
                continue
            if not data:
                continue

            if is_image_attachment(filename, mime_type):
                image_parts.append((data, mime_type or "image/jpeg"))
                continue

            text = extract_attachment_text(filename, mime_type, data)
            if text:
                text_chunks.append(f"--- Attachment: {filename} ---\n{text}")

        return "\n\n".join(text_chunks), image_parts

    def _execute_sync_pipelines(
        self,
        database: DatabaseManager,
        sheets_client: SheetsClient,
        report: RunReport,
        run_start: datetime | None = None,
        *,
        calendar_dry_run: bool = False,
        calendar_rebuild: bool = False,
    ) -> None:
        sheets_sync_successful = False
        logger.info("[SYNC] Starting Sheet Sync")
        try:
            sheets_client.sync.sync_active_opportunities(database, run_start=run_start)
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

        self._execute_calendar_sync(
            database, report, calendar_dry_run=calendar_dry_run, calendar_rebuild=calendar_rebuild
        )

        logger.info("Checking for upcoming deadlines and events")
        try:
            from placement_mail_tracker.scheduler.alert_generator import AlertGenerator

            alert_generator = AlertGenerator(database, self.settings)
            alert_generator.check_and_send_alerts()
        except Exception as e:
            logger.exception("Alert generation failed: %s", e)
            report.mark_component("notifications", False, str(e), critical=False)

    def _execute_calendar_sync(
        self,
        database: DatabaseManager,
        report: RunReport,
        *,
        calendar_dry_run: bool = False,
        calendar_rebuild: bool = False,
    ) -> None:
        """Sync the "VIT Placements" Google Calendar (ADR docs/design/03-adr-calendar-sync.md).

        Gated on ``settings.calendar_sync_enabled`` OR an explicit CLI flag
        (spec §4 point 3) so ``--calendar-dry-run``/``--calendar-rebuild`` work
        for manual verification even before the feature is flipped on. Always
        non-critical on failure — the calendar is enrichment; mail ingestion
        and the sheet must survive its death (ADR Decision 5).
        """
        if not (self.settings.calendar_sync_enabled or calendar_dry_run or calendar_rebuild):
            return

        logger.info("[SYNC] Starting Calendar Sync")
        try:
            from placement_mail_tracker.calendar_sync.client import GoogleCalendarClient
            from placement_mail_tracker.calendar_sync.sync import CalendarSyncEngine
            from placement_mail_tracker.scheduler.calendar_flags_store import (
                append_calendar_flags,
            )

            calendar_client = GoogleCalendarClient(self.settings)
            calendar_engine = CalendarSyncEngine(database, calendar_client, self.settings)

            if calendar_rebuild:
                result = calendar_engine.rebuild()
            else:
                result = calendar_engine.sync(dry_run=calendar_dry_run)

            logger.info(
                "[SYNC] Calendar sync: inserted=%s patched=%s unchanged=%s "
                "marked_done=%s retitled_stale=%s dry_run=%s",
                result.inserted, result.patched, result.unchanged,
                result.marked_done, result.retitled_stale, result.dry_run,
            )
            for line in result.flagged:
                logger.info("[CALENDAR] %s", line)
            if result.flagged and not calendar_dry_run:
                append_calendar_flags(result.flagged)
        except Exception as e:
            logger.exception("[SYNC] Calendar Sync Failed: %s", e)
            report.mark_component("calendar", False, str(e), critical=False)

    def _finalize_report(
        self, report: RunReport, stats: dict[str, int], fetch_started_at: str
    ) -> None:
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

        # Only advance the fetch window when Gmail was actually reachable.
        # Persisting the timestamp captured *before* the fetch (not "now")
        # guarantees emails that arrived mid-run are still seen next time.
        if report.gmail_ok:
            fetch_state_path = Path(self.settings.fetch_state_file)
            fetch_state_path.parent.mkdir(parents=True, exist_ok=True)
            fetch_state_path.write_text(
                json.dumps({"last_successful_fetch": fetch_started_at}),
                encoding="utf-8",
            )
        else:
            logger.warning(
                "Gmail fetch did not succeed; leaving fetch window unchanged "
                "so no emails are skipped on the next run"
            )
