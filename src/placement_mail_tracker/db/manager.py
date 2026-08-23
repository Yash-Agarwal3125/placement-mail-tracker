"""Reusable SQLite database manager for Placement Mail Tracker.

Drive-centric architecture: each placement drive is a tracked entity.
Follow-up emails update the existing drive instead of inserting duplicates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.extraction.rule_engine import (
    _PLACEMENT_PROCESS_CLASSIFICATIONS,
    normalize_company_name,
)
from placement_mail_tracker.utils.time import parse_datetime_flexible, utc_now_iso

if TYPE_CHECKING:
    from placement_mail_tracker.calendar_sync.derive import CalendarEvent

logger = logging.getLogger(__name__)

OPPORTUNITY_FIELDS = (
    "company_name",
    "role",
    "internship_or_fulltime",
    "package_or_stipend",
    "eligibility",
    "cgpa_requirement",
    "branches_allowed",
    "deadline",
    "interview_date",
    "oa_date",
    "registration_link",
    "work_location",
    "hiring_process",
    "important_notes",
)

JSON_FIELDS = {"branches_allowed", "hiring_process", "important_notes", "validation_flags"}

VALID_STATUSES = (
    "OPEN",
    "REGISTERED",
    "SHORTLISTED",
    "OA",
    "INTERVIEW",
    "HR",
    "SELECTED",
    "OFFER_RECEIVED",
    "REJECTED",
    "WITHDRAWN",
    "EXPIRED",
    "COMPLETED",
)

# Cap on stored status-history entries per drive (prevents unbounded growth).
MAX_STATUS_HISTORY = 20

# current_status values that mean "this drive is still live" -- used by
# fetch_active_drives_only (dashboard/sheet display: "should this show as an
# open opportunity").
ACTIVE_CURRENT_STATUSES = (
    "OPEN",
    "REGISTERED",
    "SHORTLISTED",
    "OA",
    "INTERVIEW",
    "HR",
    "SELECTED",
)

# current_status values a process/confirmation mail may legitimately attach
# to (docs/design/16 Cause 1 follow-up). Deliberately wider than
# ACTIVE_CURRENT_STATUSES by one value: EXPIRED means the *registration
# window* closed, not that the drive's selection process ended -- a
# confirmation or stage-update mail routinely arrives weeks after
# registration closes, while OA/interview rounds are still running. Found
# empirically: 97 of 215 live opportunities sit at EXPIRED, and every one of
# them was invisible to the resolver, so process mail that didn't happen to
# match by thread_id/hash silently minted a duplicate row instead of
# attaching (Accenture: 11 confirmation mails, 11 phantom duplicate drives,
# despite opportunity id=243 already existing). Kept as a separate constant
# rather than widening ACTIVE_CURRENT_STATUSES itself, since that constant's
# other consumer (fetch_active_drives_only) means something different --
# "is this still worth showing as an open opportunity" -- where EXPIRED
# correctly stays excluded.
RESOLVER_CANDIDATE_STATUSES = ACTIVE_CURRENT_STATUSES + ("EXPIRED",)


def _get_year_from_opportunity(opportunity: dict[str, Any]) -> str:
    email_received_at = opportunity.get("email_received_at")
    if email_received_at:
        try:
            # handle both "2026-12-31T23:59:59" and "%d-%b-%Y %I:%M %p" formats
            if "T" in email_received_at:
                return str(datetime.fromisoformat(email_received_at).year)
            # Try to parse the human readable format used by runner.py
            return str(datetime.strptime(email_received_at, "%d-%b-%Y %I:%M %p").year)
        except Exception:
            pass
    return str(datetime.now().year)

def generate_unique_hash(opportunity: dict[str, Any]) -> str:
    """Generate a stable hash for duplicate prevention by drive.

    Uses company + role + type + year. Intentionally excludes package_or_stipend
    because that field is mutable (follow-up emails often add/correct it), and
    including it causes follow-ups to mint duplicate drives instead of updating.
    """
    year = _get_year_from_opportunity(opportunity)

    parts = [
        _normalize_key(opportunity.get("company_name", "")),
        _normalize_key(opportunity.get("role", "")),
        _normalize_key(opportunity.get("internship_or_fulltime", "")),
        year,
    ]
    normalized = "::".join(parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_drive_id(
    company_name: str,
    role: str | None = None,
    category: str | None = None,
    year: str | None = None,
) -> str:
    """Generate a human-readable drive ID.

    Examples: MICROSOFT_2027_DS_INTERN, DELL_2027_SUMMER_INTERN
    """
    year = year or str(datetime.now().year)

    # Normalize company to uppercase slug
    normalized = normalize_company_name(company_name)
    slug = normalized.upper().replace(" ", "").replace("-", "")

    # Build role slug
    role_slug = ""
    if role:
        role_words = role.upper().split()[:3]  # first 3 words
        role_slug = "_".join(w for w in role_words if len(w) > 1)

    # Build category slug
    cat_slug = ""
    if category:
        cat_map = {
            "internship": "INTERN",
            "full_time": "FTE",
            "contract": "CONTRACT",
        }
        cat_slug = cat_map.get(category.lower(), category.upper()[:6])

    parts = [slug, str(year)]
    if role_slug:
        parts.append(role_slug)
    if cat_slug:
        parts.append(cat_slug)

    return "_".join(parts)

class DatabaseManager:
    """High-level SQLite manager for the drive-centric placement tracker."""

    def __init__(
        self,
        database_path: Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None and database_path is None:
            msg = "Provide either database_path or connection."
            raise ValueError(msg)

        self.connection = connection or get_connection(database_path)  # type: ignore[arg-type]
        self.create_tables()

    def create_tables(self) -> None:
        """Create all required SQLite tables and indexes automatically."""
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                total_drives INTEGER NOT NULL DEFAULT 0,
                active_drives INTEGER NOT NULL DEFAULT 0,
                selected_drives INTEGER NOT NULL DEFAULT 0,
                rejected_drives INTEGER NOT NULL DEFAULT 0,
                last_activity TEXT
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_hash TEXT UNIQUE NOT NULL,
                company_name TEXT NOT NULL,
                role TEXT NOT NULL,
                internship_or_fulltime TEXT,
                package_or_stipend TEXT,
                eligibility TEXT,
                cgpa_requirement TEXT,
                branches_allowed TEXT NOT NULL DEFAULT '[]',
                deadline TEXT,
                interview_date TEXT,
                oa_date TEXT,
                registration_link TEXT,
                work_location TEXT,
                hiring_process TEXT NOT NULL DEFAULT '[]',
                important_notes TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                source_email_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                current_status TEXT NOT NULL DEFAULT 'OPEN',
                status_history TEXT NOT NULL DEFAULT '[]',
                last_update_timestamp TEXT,
                email_received_at TEXT,
                drive_id TEXT,
                source_thread_id TEXT,
                action_required TEXT,
                email_classification TEXT,
                my_status TEXT NOT NULL DEFAULT 'NOT_APPLIED',
                next_event_date TEXT,
                eligibility_status TEXT NOT NULL DEFAULT 'MANUAL_REVIEW',
                applied_date TEXT,
                priority TEXT NOT NULL DEFAULT 'MEDIUM',
                degree_level TEXT NOT NULL DEFAULT 'UNKNOWN',
                dream_category TEXT NOT NULL DEFAULT 'NORMAL',
                validation_flags TEXT NOT NULL DEFAULT '[]',
                drive_kind TEXT NOT NULL DEFAULT 'PLACEMENT'
            );

            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                update_type TEXT NOT NULL,
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT UNIQUE NOT NULL,
                opportunity_id INTEGER,
                subject TEXT NOT NULL,
                sender TEXT,
                received_at TEXT,
                filter_score INTEGER,
                filter_decision TEXT,
                processed_status TEXT NOT NULL,
                error_message TEXT,
                email_classification TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_retry_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                channel TEXT NOT NULL,
                recipient TEXT,
                subject TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS sent_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(opportunity_id, alert_type),
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                drive_id TEXT,
                event_type TEXT NOT NULL,
                gcal_calendar_id TEXT,
                gcal_event_id TEXT,
                start_iso TEXT NOT NULL,
                end_iso TEXT NOT NULL,
                all_day INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                location TEXT,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_seen_active_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(opportunity_id, event_type),
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS unmatched_confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT NOT NULL,
                extracted_text TEXT,
                candidates TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS roster_verdicts (
                opportunity_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                verdict TEXT NOT NULL,
                method TEXT NOT NULL,
                score REAL,
                source_email_id TEXT,
                verified_at TEXT NOT NULL,
                PRIMARY KEY (opportunity_id, event_type),
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );
            """
        )
        self._migrate_existing_opportunities_table()
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_opportunities_status
                ON opportunities(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunities_unique_hash
                ON opportunities(unique_hash);
            CREATE INDEX IF NOT EXISTS idx_opportunities_drive_id
                ON opportunities(drive_id);
            CREATE INDEX IF NOT EXISTS idx_opportunities_thread_id
                ON opportunities(source_thread_id);
            CREATE INDEX IF NOT EXISTS idx_opportunities_company_name
                ON opportunities(company_name);
            CREATE INDEX IF NOT EXISTS idx_updates_opportunity_id
                ON updates(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_processed_emails_message_id
                ON processed_emails(gmail_message_id);
            CREATE INDEX IF NOT EXISTS idx_processed_emails_status
                ON processed_emails(processed_status);
            CREATE INDEX IF NOT EXISTS idx_notifications_opportunity_id
                ON notifications(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_calendar_events_status
                ON calendar_events(status);
            CREATE INDEX IF NOT EXISTS idx_calendar_events_opportunity_id
                ON calendar_events(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_unmatched_confirmations_message_id
                ON unmatched_confirmations(gmail_message_id);
            """
        )
        self.connection.commit()

    # ------------------------------------------------------------------
    # Drive-Centric Insert/Update
    # ------------------------------------------------------------------

    def insert_or_update_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None = None,
        source_thread_id: str | None = None,
        email_classification: str | None = None,
        matched_opportunity_id: int | None = None,
    ) -> tuple[int | None, bool]:
        """Insert a new drive or update an existing one.

        Follow-up detection (Phase 2):
        1. Match by thread_id first (bulletproof for Gmail threads)
        2. Fallback to hash matching (company + role + package + year)

        ``matched_opportunity_id`` (doc 15 §1.4-B) makes the fuzzy matcher's
        result *authoritative*: when the caller already knows (via
        ``utils.deduplication.find_best_match``) which existing row this
        opportunity belongs to, pass its id here to route straight to an
        update of that row. This bypasses the exact-hash gate, which is
        blind to a differing ``internship_or_fulltime`` (or any other
        hashed field) even when the fuzzy matcher correctly identified the
        same underlying drive.

        Cause 1 (calendar-drift remediation, Phase 1): a mail classified as
        one of ``_PLACEMENT_PROCESS_CLASSIFICATIONS`` (OA_UPDATE,
        SHORTLIST_UPDATE, INTERVIEW_UPDATE, OFFER_UPDATE,
        APPLICATION_CONFIRMATION) is by definition about a drive that
        already exists -- ``role`` is re-extracted per mail and is too
        unstable to hash on (see ``generate_unique_hash``), so a process
        mail that doesn't already match by thread/hash/fuzzy match falls
        through to :meth:`_resolve_process_mail_target` before any insert
        is considered. When that resolves to more than one live candidate,
        this method attaches to **none of them** -- it logs a warning with
        every candidate id and (when ``source_email_id`` is available)
        writes an ``unmatched_confirmations`` row, returning
        ``(None, False)``. Silent mis-attachment is worse than a missing
        event.

        Returns ``(opportunity_id, created_new_record)``, or ``(None,
        False)`` when a process mail was ambiguous and routed to review.
        """
        normalized = self._normalize_opportunity(opportunity)
        existing = None

        # 0. Fuzzy match already resolved identity — authoritative (§1.4-B).
        if matched_opportunity_id is not None:
            existing = self.fetch_opportunity_by_id(matched_opportunity_id)

        # 1. Match by thread ID first (Follow-up detection)
        if not existing and source_thread_id:
            existing = self.fetch_opportunity_by_thread_id(source_thread_id)

        # 2. Fallback to hash matching
        if not existing:
            existing = self.find_duplicate_opportunity(normalized)

        # 3/4. Process mails attach to an existing drive, never create one
        # (Cause 1). Only reached once thread/hash/fuzzy match all missed.
        ambiguous_candidates: list[sqlite3.Row] = []
        if not existing and email_classification in _PLACEMENT_PROCESS_CLASSIFICATIONS:
            resolved, ambiguous_candidates = self._resolve_process_mail_target(normalized)
            if resolved is not None:
                existing = resolved
                logger.info(
                    "Process mail (%s) for %r attached to existing drive id=%s (%r / %r)",
                    email_classification,
                    normalized.get("company_name"),
                    resolved["id"],
                    resolved["company_name"],
                    resolved["role"],
                )
        elif not existing and email_classification == "IRRELEVANT":
            # Fail-closed backstop (2026-08-23 audit) for exactly the failure
            # class that produced the PPT/assignment/bare-deadline/selection-
            # process/selection-list/written-test bugs: a real placement mail
            # whose phrasing doesn't (yet) match any pattern in rule_engine's
            # _CLASSIFICATION_PATTERNS classifies IRRELEVANT and, before this
            # change, fell straight through to the unconditional insert below
            # -- minting a duplicate, dateless drive instead of updating the
            # real one. Every one of those bugs was only ever caught after
            # the fact by re-diffing the whole historical corpus by hand.
            #
            # Deliberately scoped to the literal "IRRELEVANT" classification
            # only -- NOT "any classification other than NEW_DRIVE", which
            # also catches email_classification=None (no classification was
            # even attempted: exercised throughout the test suite and by
            # scripts that insert opportunities directly) and would block
            # perfectly legitimate inserts having nothing to do with this bug
            # class.
            #
            # This does NOT silently attach (an ambiguous or even a single
            # match here is not proof the mail is a follow-up the way a
            # genuine process classification is -- it could be a real second
            # concurrent drive at the same company). It only ever prevents a
            # silent create: when a same-company+year active drive already
            # exists, park it in unmatched_confirmations for a human to
            # decide, via the same review path §1.4-B's ambiguous case
            # already uses, instead of minting a duplicate. A genuinely new
            # company (no existing active row) still falls through to a
            # normal insert below, unchanged -- this costs nothing for the
            # overwhelming common case.
            candidate, ambiguous_candidates = self._resolve_process_mail_target(normalized)
            if candidate is not None:
                ambiguous_candidates = [candidate, *ambiguous_candidates]

        if existing is None and ambiguous_candidates:
            candidate_ids = [c["id"] for c in ambiguous_candidates]
            logger.warning(
                "Mail (%s) for %r / %r matched %d active candidate(s) %s for "
                "the year -- not attaching to any, routing to "
                "unmatched_confirmations",
                email_classification,
                normalized.get("company_name"),
                normalized.get("role"),
                len(ambiguous_candidates),
                candidate_ids,
            )
            if source_email_id:
                self.insert_unmatched_confirmation(
                    gmail_message_id=source_email_id,
                    extracted_text=(
                        f"{normalized.get('company_name')} / {normalized.get('role')}: "
                        f"mail ({email_classification}) matched "
                        f"{len(candidate_ids)} active candidate(s) {candidate_ids} for "
                        "the year -- ambiguous or unattached, not attached to any."
                    ),
                    candidates=[dict(c) for c in ambiguous_candidates],
                )
            return None, False

        # Merge status history
        new_status = (normalized.get("current_status") or "OPEN").upper()
        if existing:
            try:
                history = json.loads(existing["status_history"])
            except Exception:
                history = []

            # Protect against hard regression to OPEN: once a drive has advanced
            # past OPEN (e.g. OA, SHORTLISTED), a mass-announcement keyword like
            # "congratulations" that resolves to OPEN must not downgrade it.
            existing_status = (existing["current_status"] or "OPEN").upper()
            if existing_status != "OPEN" and new_status == "OPEN":
                new_status = existing_status

            if new_status and (not history or history[-1] != new_status):
                history.append(new_status)
            # Bound the history so a long-running, chatty thread can't grow it
            # without limit (we observed 38-entry flapping arrays in the wild).
            history = history[-MAX_STATUS_HISTORY:]
            normalized["status_history"] = json.dumps(history)
            normalized["current_status"] = new_status
        else:
            normalized["status_history"] = json.dumps([new_status])
            normalized["current_status"] = new_status

        if email_classification:
            normalized["email_classification"] = email_classification

        normalized["action_required"] = self._compute_action_required(normalized)

        if existing is None:
            opportunity_id = self._insert_opportunity(
                normalized,
                source_email_id=source_email_id,
                source_thread_id=source_thread_id,
            )
            self.create_update_event(
                opportunity_id,
                "created",
                notes="Drive created from email",
            )
            self._update_company_stats(normalized["company_name"])
            logger.info("Inserted drive %s (id=%s)", normalized.get("drive_id"), opportunity_id)
            if source_email_id:
                self.resolve_unmatched_confirmations_for_message(source_email_id)
            return opportunity_id, True

        opportunity_id = int(existing["id"])
        changed_fields = self._changed_fields(existing, normalized)

        if changed_fields:
            self.update_opportunity(
                opportunity_id,
                normalized,
                source_email_id=source_email_id,
                source_thread_id=source_thread_id,
                changed_fields=changed_fields,
            )
            self._update_company_stats(normalized["company_name"])
            logger.info(
                "Updated drive %s (id=%s) with %s changes",
                existing["drive_id"],
                opportunity_id,
                len(changed_fields),
            )
        else:
            self.create_update_event(
                opportunity_id,
                "duplicate_seen",
                notes="Follow-up email without drive changes",
            )
            logger.info("Follow-up for drive %s had no changes", existing["drive_id"])

        if source_email_id:
            self.resolve_unmatched_confirmations_for_message(source_email_id)
        return opportunity_id, False

    def update_opportunity(
        self,
        opportunity_id: int,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None = None,
        source_thread_id: str | None = None,
        changed_fields: list[tuple[str, Any, Any]] | None = None,
    ) -> None:
        """Update one drive and create update-history rows."""
        normalized = self._normalize_opportunity(opportunity)
        existing = self.fetch_opportunity_by_id(opportunity_id)
        if existing is None:
            msg = f"Opportunity {opportunity_id} was not found."
            raise ValueError(msg)

        changes = changed_fields or self._changed_fields_from_dict(existing, normalized)
        self._update_opportunity_row(
            opportunity_id,
            normalized,
            source_email_id=source_email_id,
            source_thread_id=source_thread_id,
        )

        for field_name, old_value, new_value in changes:
            self.create_update_event(
                opportunity_id,
                "updated",
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )

    # ------------------------------------------------------------------
    # Process-mail attach resolution (Cause 1 / Phase 1)
    # ------------------------------------------------------------------

    def _resolve_process_mail_target(
        self, normalized: dict[str, Any]
    ) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
        """Resolve a process-classification mail to an existing drive.

        Only called once thread_id/hash/fuzzy match have all missed.
        Candidate pool is RESOLVER_CANDIDATE_STATUSES, not
        ACTIVE_CURRENT_STATUSES -- a process/confirmation mail routinely
        arrives after a drive's registration window (current_status=EXPIRED)
        has closed, while its OA/interview rounds are still running. Tie-
        break order: exactly one active same-normalised-company candidate
        for the year -> compatible program name. Returns
        ``(resolved_row_or_None, candidates_considered)`` -- the second
        element is populated only when resolution failed with two or more
        surviving candidates, so the caller can log/route them for review.
        Never guesses when more than one candidate remains after both
        steps (a Flipkart mail has already been mis-filed under ProcDNA in
        production this way).
        """
        company = normalized.get("company_name")
        norm_company = normalize_company_name(company) if company else ""
        if not norm_company:
            return None, []

        year = _get_year_from_opportunity(normalized)
        placeholders = ", ".join("?" for _ in RESOLVER_CANDIDATE_STATUSES)
        rows = self.connection.execute(
            f"""
            SELECT * FROM opportunities
            WHERE status = 'active' AND current_status IN ({placeholders});
            """,
            RESOLVER_CANDIDATE_STATUSES,
        ).fetchall()

        candidates = [
            row
            for row in rows
            # .casefold(): normalize_company_name() preserves an all-caps
            # single token as a presumed acronym (rule_engine._smart_title),
            # so "GROWW" (shouting its own name, not an acronym) and "Groww"
            # normalize to two different strings and an exact match here
            # misses them -- confirmed live as opportunities 13/54 (2026-08-23
            # audit). Casefolding the comparison (not the stored/display
            # value) closes that gap without touching real acronym display.
            if (normalize_company_name(row["company_name"]) or "").casefold()
            == norm_company.casefold()
            and _get_year_from_opportunity(dict(row)) == year
        ]

        if not candidates:
            return None, []
        if len(candidates) == 1:
            return candidates[0], []

        narrowed = self._filter_by_compatible_program(candidates, normalized.get("role"))
        if len(narrowed) == 1:
            return narrowed[0], []

        return None, narrowed

    @staticmethod
    def _filter_by_compatible_program(
        candidates: list[sqlite3.Row], incoming_role: str | None
    ) -> list[sqlite3.Row]:
        """Program-name tie-breaker: the simplest rule that discriminates.

        Substring containment on the normalised role text, either
        direction ("Super Dream Internship" vs "Super Dream PPO" does NOT
        contain either way and stays ambiguous; "Online Test is scheduled"
        style role text that happens to *repeat* an existing program name
        does). Deliberately not a fuzzy/token-overlap scorer: an
        under-matching rule fails safe (falls through to the review
        queue); an over-matching one risks exactly the silent mis-attach
        this phase exists to prevent.
        """
        incoming_norm = _normalize_key(incoming_role)
        if not incoming_norm:
            return candidates
        compatible = []
        for row in candidates:
            row_norm = _normalize_key(row["role"])
            if row_norm and (row_norm in incoming_norm or incoming_norm in row_norm):
                compatible.append(row)
        return compatible or candidates

    # ------------------------------------------------------------------
    # Action Required Engine
    # ------------------------------------------------------------------

    def _compute_action_required(self, opportunity: dict[str, Any]) -> str | None:
        """Compute the action required based on deadlines and status."""
        status = (opportunity.get("current_status") or "").upper()
        deadline = opportunity.get("deadline")
        oa_date = opportunity.get("oa_date")
        interview_date = opportunity.get("interview_date")

        now = datetime.now()
        tomorrow = now + timedelta(days=1)

        # Extracted dates are rarely ISO ("15 June 2026", "15-Jun-2026 10:30 AM",
        # etc.), so parse them flexibly instead of with datetime.fromisoformat.
        def _is_tomorrow(date_str: str | None) -> bool:
            if not date_str:
                return False
            dt = parse_datetime_flexible(date_str)
            return dt is not None and dt.date() == tomorrow.date()

        def _is_today(date_str: str | None) -> bool:
            if not date_str:
                return False
            dt = parse_datetime_flexible(date_str)
            return dt is not None and dt.date() == now.date()

        role = (opportunity.get("role") or "").strip()
        if not role or role == "Unknown Role":
            return "VERIFY ROLE"

        if status == "OFFER_RECEIVED":
            return "REVIEW OFFER"
        if _is_today(deadline) or _is_tomorrow(deadline):
            return "APPLY TODAY"
        if _is_today(oa_date) or _is_tomorrow(oa_date):
            return "PREPARE FOR TEST"
        if _is_today(interview_date) or _is_tomorrow(interview_date):
            return "PREPARE FOR INTERVIEW"
        if status == "OPEN" and deadline:
            return "REGISTER BEFORE DEADLINE"

        return None

    # ------------------------------------------------------------------
    # Event/Update Tracking
    # ------------------------------------------------------------------

    def create_update_event(
        self,
        opportunity_id: int,
        update_type: str,
        *,
        field_name: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
        notes: str | None = None,
    ) -> int:
        """Create one row in the updates table."""
        cursor = self.connection.execute(
            """
            INSERT INTO updates (
                opportunity_id, update_type, field_name,
                old_value, new_value, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                opportunity_id,
                update_type,
                field_name,
                _serialize_value(old_value),
                _serialize_value(new_value),
                notes,
                utc_now_iso(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # Query Methods
    # ------------------------------------------------------------------

    # Upgrade-only ladder for source="automation" writes (D4, docs/design/10-
    # confirmation-and-reminders.md). SELECTED/REJECTED share a rank: both are
    # terminal outcomes, neither is an "upgrade" over the other, but either is
    # an upgrade over SHORTLISTED/APPLIED/NOT_APPLIED.
    MY_STATUS_LADDER: dict[str, int] = {
        "NOT_APPLIED": 0,
        "APPLIED": 1,
        "SHORTLISTED": 2,
        "SELECTED": 3,
        "REJECTED": 3,
    }

    def set_my_status(self, drive_id: str, my_status: str, *, source: str = "sheet") -> bool:
        """Source-aware My Status write — the single choke point (D4).

        source="sheet" (default) is the existing, fully-authoritative human
        path: the sheet read-back may set any value, including a downgrade —
        unchanged from the original bulk_update_my_status behaviour.
        source="automation" enforces an upgrade-only ladder: a write is only
        applied when it strictly advances rank over the current value, and a
        drive already at or above the target rank is a no-op. This is what
        makes a duplicate or late confirmation mail idempotent for free (D6).

        Targets a single row (the most recently updated one sharing
        ``drive_id``), never every row matching ``drive_id``. ``drive_id``
        has no UNIQUE constraint, and Phase 1 (Cause 1) removed the
        ``_insert_opportunity`` suffix logic that used to force it unique --
        two genuinely distinct new drives can now share a generated
        ``drive_id`` (same company/first-3-role-words/year). An unscoped
        ``WHERE drive_id = ?`` would silently write ``my_status`` onto both.

        Returns True if a row was actually changed, False on any no-op.
        """
        if not drive_id or not my_status:
            return False

        target = self.connection.execute(
            "SELECT id, my_status FROM opportunities WHERE drive_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1;",
            (drive_id,),
        ).fetchone()
        if target is None:
            return False

        if source == "automation":
            current_rank = self.MY_STATUS_LADDER.get(target["my_status"] or "NOT_APPLIED", 0)
            target_rank = self.MY_STATUS_LADDER.get(my_status, 0)
            if target_rank <= current_rank:
                return False

        cursor = self.connection.execute(
            "UPDATE opportunities SET my_status = ? WHERE id = ? AND my_status != ?;",
            (my_status, target["id"], my_status),
        )
        if cursor.rowcount:
            self.connection.commit()
        return cursor.rowcount > 0

    def bulk_update_my_status(
        self, my_status_by_drive_id: dict[str, str], *, source: str = "sheet"
    ) -> int:
        """Write multiple My Status values back into the DB via set_my_status.

        Called by the sheets sync right after it reads the current My Status
        values back from the sheet (ADR-D8 / Decision 6): the DB becomes the
        durable copy of user intent instead of a dead column that always
        reads NOT_APPLIED. Returns the changed-row count.
        """
        changed = 0
        for drive_id, my_status in my_status_by_drive_id.items():
            if self.set_my_status(drive_id, my_status, source=source):
                changed += 1
        return changed

    # ------------------------------------------------------------------
    # Confirmation-mail matching gaps (docs/design/08-confirmation-audit.md
    # C3, docs/design/10-confirmation-and-reminders.md): a confirmation mail
    # that matches no drive at all, or matches ambiguously, is never
    # silent-dropped -- it lands here for a human to review.
    # ------------------------------------------------------------------

    def insert_unmatched_confirmation(
        self,
        *,
        gmail_message_id: str,
        extracted_text: str | None,
        candidates: list[dict[str, Any]],
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO unmatched_confirmations
                (gmail_message_id, extracted_text, candidates, created_at)
            VALUES (?, ?, ?, ?);
            """,
            (gmail_message_id, extracted_text, json.dumps(candidates), utc_now_iso()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def fetch_unmatched_confirmations(
        self, *, unresolved_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return unmatched_confirmations rows (safety-nets plan Phase 1).

        ``unresolved_only=True`` is what the daily digest uses -- otherwise
        every historical row (265+ in production) would resurface on every
        run forever, training a reader to ignore the section.
        """
        query = "SELECT * FROM unmatched_confirmations"
        if unresolved_only:
            query += " WHERE resolved = 0"
        query += " ORDER BY created_at DESC;"
        rows = self.connection.execute(query).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["candidates"] = json.loads(item["candidates"])
            except Exception:
                item["candidates"] = []
            results.append(item)
        return results

    def resolve_unmatched_confirmations_for_message(self, gmail_message_id: str) -> int:
        """Mark any unresolved unmatched_confirmations row(s) for this
        Gmail message as resolved (safety-nets plan Phase 1).

        Called when ``insert_or_update_opportunity`` successfully attaches
        (or creates) a drive for ``source_email_id`` -- most of the
        historical unmatched rows are superseded once the same mail is
        later reprocessed (e.g. by a backfill) and resolves cleanly.
        Returns the number of rows changed.
        """
        if not gmail_message_id:
            return 0
        cursor = self.connection.execute(
            "UPDATE unmatched_confirmations SET resolved = 1, resolved_at = ? "
            "WHERE gmail_message_id = ? AND resolved = 0;",
            (utc_now_iso(), gmail_message_id),
        )
        if cursor.rowcount:
            self.connection.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Calendar Sync state (docs/design/04-integration-spec.md §3.3)
    # ------------------------------------------------------------------

    def upsert_calendar_event_state(
        self,
        event: CalendarEvent,
        *,
        gcal_calendar_id: str,
        gcal_event_id: str | None,
        status: str = "active",
    ) -> int:
        """Insert or update the state row for one (opportunity_id, event_type).

        Pattern mirrors ``log_processed_email``'s ON CONFLICT DO UPDATE. Returns
        the row id; fetched via a follow-up SELECT rather than
        ``cursor.lastrowid`` because SQLite does not reliably report the
        rowid of a row touched only by the DO UPDATE branch of an upsert.
        """
        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO calendar_events (
                opportunity_id, drive_id, event_type, gcal_calendar_id, gcal_event_id,
                start_iso, end_iso, all_day, title, location, content_hash, status,
                last_seen_active_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_id, event_type) DO UPDATE SET
                drive_id = excluded.drive_id,
                gcal_calendar_id = excluded.gcal_calendar_id,
                gcal_event_id = excluded.gcal_event_id,
                start_iso = excluded.start_iso,
                end_iso = excluded.end_iso,
                all_day = excluded.all_day,
                title = excluded.title,
                location = excluded.location,
                content_hash = excluded.content_hash,
                status = excluded.status,
                last_seen_active_at = excluded.last_seen_active_at,
                updated_at = excluded.updated_at;
            """,
            (
                event.opportunity_id,
                event.drive_id,
                event.event_type,
                gcal_calendar_id,
                gcal_event_id,
                event.start_iso,
                event.end_iso,
                int(event.all_day),
                event.title,
                event.location,
                event.content_hash(),
                status,
                now,
                now,
                now,
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT id FROM calendar_events WHERE opportunity_id = ? AND event_type = ?;",
            (event.opportunity_id, event.event_type),
        ).fetchone()
        return int(row["id"])

    def fetch_calendar_event_states(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """Return calendar_events rows, optionally filtered by status."""
        if status is None:
            rows = self.connection.execute("SELECT * FROM calendar_events;").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM calendar_events WHERE status = ?;", (status,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_calendar_event_status(self, row_id: int, status: str) -> None:
        """Update one calendar_events row's status (done/stale/cancelled/active)."""
        self.connection.execute(
            "UPDATE calendar_events SET status = ?, updated_at = ? WHERE id = ?;",
            (status, utc_now_iso(), row_id),
        )

    def mark_calendar_event_deleted(self, row_id: int, status: str) -> None:
        """Record that a row's Google event was actually deleted.

        Clears ``gcal_event_id`` so a later reactivation (the exclusion
        being corrected) knows there is nothing left on Google to PATCH and
        must insert a fresh event instead (calendar_sync/sync.py's
        reactivation branch checks this).

        Commits immediately: unlike the normal sync() pipeline (where some
        later write in the same process happens to flush this), a one-off
        script that calls this and nothing else would otherwise lose the
        write when the process exits -- the Google deletion already
        happened and is not reversible, so the local record must not
        silently fail to follow it. (Found the hard way: the first
        production run of delete_excluded_calendar_events.py deleted 16
        real events but left all 16 rows still pointing at the now-gone
        gcal_event_id, because nothing else in that script's process ever
        committed.)
        """
        self.connection.execute(
            "UPDATE calendar_events SET status = ?, gcal_event_id = NULL, "
            "updated_at = ? WHERE id = ?;",
            (status, utc_now_iso(), row_id),
        )
        self.connection.commit()

    def upsert_roster_verdict(
        self,
        opportunity_id: int,
        event_type: str,
        verdict: str,
        *,
        method: str,
        score: float | None = None,
        source_email_id: str | None = None,
    ) -> None:
        """Record a per-round roster verdict (docs/design/16 Phase C).

        Keyed by (opportunity_id, event_type), matching ``calendar_events``'s
        own granularity, so being MATCHED for OA carries no implication for
        INTERVIEW -- a direct MATCHED verdict is only ever evidence for its
        own round. Cause 2 / Phase 3 (calendar-drift remediation plan) made
        the *storage* here still strictly per-round, but NOT_MATCHED is no
        longer read back as round-independent: ``calendar_sync.derive.
        is_round_excluded`` cascades a stored NOT_MATCHED forward to every
        later round of the same drive (OA -> INTERVIEW -> OFFER) unless that
        later round has its own direct MATCHED verdict. A later mail's
        verdict replaces the
        stored one -- *unless* the new verdict is AMBIGUOUS, which never
        overwrites an existing MATCHED/NOT_MATCHED. A stronger verdict (from
        a roster that actually parsed, or a direct user assertion) must not
        be silently erased by a later mail whose attachment didn't parse --
        that would be the exact non-downgrade bug already fixed for
        drive_kind and the my_status ladder, recurring here.
        """
        self.connection.execute(
            """
            INSERT INTO roster_verdicts
                (opportunity_id, event_type, verdict, method, score, source_email_id, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_id, event_type) DO UPDATE SET
                verdict = excluded.verdict,
                method = excluded.method,
                score = excluded.score,
                source_email_id = excluded.source_email_id,
                verified_at = excluded.verified_at
            WHERE excluded.verdict NOT IN ('AMBIGUOUS', 'NO_ROSTER');
            """,
            (opportunity_id, event_type, verdict, method, score, source_email_id, utc_now_iso()),
        )
        self.connection.commit()

    def fetch_roster_verdicts(self) -> dict[tuple[int, str], dict[str, Any]]:
        """Return every roster verdict, keyed by (opportunity_id, event_type)."""
        rows = self.connection.execute("SELECT * FROM roster_verdicts;").fetchall()
        return {(row["opportunity_id"], row["event_type"]): dict(row) for row in rows}
        self.connection.commit()

    def find_duplicate_opportunity(self, opportunity: dict[str, Any]) -> sqlite3.Row | None:
        """Fetch a drive by the generated hash."""
        unique_hash = generate_unique_hash(opportunity)
        return self.connection.execute(
            "SELECT * FROM opportunities WHERE unique_hash = ? LIMIT 1;",
            (unique_hash,),
        ).fetchone()

    def fetch_opportunity_by_thread_id(self, thread_id: str) -> sqlite3.Row | None:
        """Fetch a drive by Gmail thread ID."""
        return self.connection.execute(
            "SELECT * FROM opportunities WHERE source_thread_id = ? LIMIT 1;",
            (thread_id,),
        ).fetchone()

    def fetch_opportunity_by_id(self, opportunity_id: int) -> dict[str, Any] | None:
        """Fetch one drive by primary key."""
        row = self.connection.execute(
            "SELECT * FROM opportunities WHERE id = ?;",
            (opportunity_id,),
        ).fetchone()
        return self._row_to_opportunity(row) if row else None

    def fetch_active_opportunities(self) -> list[dict[str, Any]]:
        """Return active drives ordered by most recently updated (Phase 8)."""
        rows = self.connection.execute(
            """
            SELECT *
            FROM opportunities
            WHERE status = 'active'
            ORDER BY updated_at DESC;
            """
        ).fetchall()
        return [self._row_to_opportunity(row) for row in rows]

    def get_active_opportunities(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for fetch_active_opportunities."""
        return self.fetch_active_opportunities()

    def fetch_active_drives_only(self) -> list[dict[str, Any]]:
        """Return only drives with active statuses."""
        placeholders = ", ".join("?" for _ in ACTIVE_CURRENT_STATUSES)
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM opportunities
            WHERE status = 'active'
              AND current_status IN ({placeholders})
            ORDER BY updated_at DESC;
            """,
            ACTIVE_CURRENT_STATUSES,
        ).fetchall()
        return [self._row_to_opportunity(row) for row in rows]

    def expire_stale_drives(self, cutoff_days: int) -> int:
        """Auto-transition dead drives to EXPIRED (VALID_STATUSES already has
        EXPIRED; nothing previously set it automatically).

        Two deterministic signals, both meaning "not for me" (a rejection/
        not-shortlisted mail never has to arrive for either):

        1. Never applied and the application deadline already passed
           (``current_status`` still OPEN/REGISTERED, ``my_status`` still
           NOT_APPLIED). No grace period — the window is objectively closed.
        2. Silent ghosting after progressing further (SHORTLISTED/OA/
           INTERVIEW/HR with no follow-up mail): the latest known date
           (deadline/oa_date/interview_date) is more than ``cutoff_days`` in
           the past.

        Either way the drive drops out of ``fetch_active_drives_only``, so
        the calendar sync's existing stale pass retitles/cancels its events
        instead of leaving them "active" (and booking new ones) forever.
        """
        now = datetime.now()
        grace_cutoff = now - timedelta(days=cutoff_days)
        count = 0
        for row in self.fetch_active_drives_only():
            current_status = (row.get("current_status") or "OPEN").upper()
            my_status = row.get("my_status") or "NOT_APPLIED"
            dates = {
                field: parse_datetime_flexible(row.get(field) or "")
                for field in ("deadline", "oa_date", "interview_date")
            }
            latest = max((d for d in dates.values() if d), default=None)

            never_applied_deadline_passed = (
                current_status in ("OPEN", "REGISTERED")
                and my_status == "NOT_APPLIED"
                and dates["deadline"] is not None
                and dates["deadline"] < now
            )
            ghosted = latest is not None and latest < grace_cutoff

            if not (never_applied_deadline_passed or ghosted):
                continue

            history = row.get("status_history") or []
            if isinstance(history, str):
                try:
                    history = json.loads(history)
                except Exception:
                    history = []
            if not history or history[-1] != "EXPIRED":
                history.append("EXPIRED")
            history = history[-MAX_STATUS_HISTORY:]

            self.connection.execute(
                "UPDATE opportunities SET current_status = 'EXPIRED', "
                "status_history = ?, updated_at = ? WHERE id = ?;",
                (json.dumps(history), utc_now_iso(), row["id"]),
            )
            count += 1

        if count:
            self.connection.commit()
        return count

    def fetch_updates_for_opportunity(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Fetch update history for one drive."""
        rows = self.connection.execute(
            """
            SELECT *
            FROM updates
            WHERE opportunity_id = ?
            ORDER BY created_at ASC, id ASC;
            """,
            (opportunity_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Dashboard Metrics
    # ------------------------------------------------------------------

    def get_dashboard_metrics(self) -> dict[str, Any]:
        """Compute dashboard metrics (Phase 10).

        "This week" counts are genuinely date-bounded (next 7 days) rather than
        a raw status tally, so the dashboard reflects what is actually coming up.
        """
        all_opps = self.fetch_active_opportunities()

        active_statuses = {"OPEN", "REGISTERED", "SHORTLISTED", "OA", "INTERVIEW", "HR"}

        active_drives = sum(1 for o in all_opps if o.get("current_status") in active_statuses)
        applications_open = sum(1 for o in all_opps if o.get("current_status") == "OPEN")
        offers = sum(1 for o in all_opps if o.get("current_status") == "OFFER_RECEIVED")
        selected = sum(
            1 for o in all_opps
            if o.get("current_status") in ("SELECTED", "OFFER_RECEIVED")
        )
        rejected = sum(1 for o in all_opps if o.get("current_status") == "REJECTED")

        total = len(all_opps)
        companies = len(set(o.get("company_name", "") for o in all_opps))
        selection_rate = f"{(offers / total * 100):.1f}%" if total > 0 else "0%"

        return {
            "active_drives": active_drives,
            "applications_open": applications_open,
            "oa_this_week": sum(1 for o in all_opps if _within_next_days(o.get("oa_date"), 7)),
            "interviews_this_week": sum(
                1 for o in all_opps if _within_next_days(o.get("interview_date"), 7)
            ),
            "deadlines_this_week": sum(
                1 for o in all_opps if _within_next_days(o.get("deadline"), 7)
            ),
            "action_required": sum(
                1 for o in all_opps if (o.get("action_required") or "").strip()
            ),
            "offers_received": offers,
            "selected": selected,
            "rejected": rejected,
            "companies_applied": companies,
            "selection_rate": selection_rate,
            "total_drives": total,
            "dead_letter_count": self.get_dead_letter_count(),
            "emails_today": self._count_emails_today(),
        }

    def get_dead_letter_count(self) -> int:
        """Count emails that permanently failed processing."""
        row = self.connection.execute(
            "SELECT COUNT(*) FROM processed_emails WHERE processed_status = 'PERMANENT_FAILURE'"
        ).fetchone()
        return row[0] if row else 0

    def get_quota_deferred_count(self) -> int:
        """Count emails currently deferred by Gemini daily-quota exhaustion.

        Distinguishes quota-caused ``PENDING_EXTRACTION`` rows (retried once
        quota frees up, per ``GeminiQuotaExhaustedError`` handling in
        ``scheduler/runner.py``) from the same status used for a generic
        transient error retry, so this stays a "quota is dead today" signal
        rather than a catch-all in-flight-retry counter.
        """
        row = self.connection.execute(
            """
            SELECT COUNT(*) FROM processed_emails
            WHERE processed_status = 'PENDING_EXTRACTION'
              AND (error_message LIKE '%quota%' OR error_message LIKE '%429%')
            """
        ).fetchone()
        return row[0] if row else 0

    def get_dead_letter_emails(self, limit: int = 3) -> list[dict]:
        """Return the most recent dead-letter emails."""
        rows = self.connection.execute(
            """
            SELECT gmail_message_id, subject, sender, received_at, error_message, created_at
            FROM processed_emails
            WHERE processed_status = 'PERMANENT_FAILURE'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_updates(self, limit: int = 20) -> list[dict]:
        """Return recent drive field changes joined with opportunity info."""
        rows = self.connection.execute(
            """
            SELECT u.field_name, u.old_value, u.new_value, u.created_at,
                   o.company_name, o.role,
                   pe.received_at AS email_received_at
            FROM updates u
            JOIN opportunities o ON o.id = u.opportunity_id
            LEFT JOIN processed_emails pe ON pe.opportunity_id = o.id
            ORDER BY u.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _count_emails_today(self) -> int:
        """Count processed emails whose created_at is today (local date)."""
        today = datetime.now().date().isoformat()
        row = self.connection.execute(
            "SELECT COUNT(*) FROM processed_emails WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Processed Emails, Notifications
    # ------------------------------------------------------------------

    def purge_old_processed_emails(self, days: int = 30) -> int:
        """Purge processed_emails older than the specified number of days."""
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = self.connection.execute(
            """
            DELETE FROM processed_emails
            WHERE created_at < ?
            """,
            (cutoff_date,)
        )
        self.connection.commit()
        return cursor.rowcount

    def log_processed_email(
        self,
        *,
        gmail_message_id: str,
        subject: str,
        sender: str | None = None,
        received_at: str | None = None,
        opportunity_id: int | None = None,
        filter_score: int | None = None,
        filter_decision: dict[str, Any] | None = None,
        processed_status: str = "processed",
        error_message: str | None = None,
        email_classification: str | None = None,
        retry_count: int | None = None,
        last_retry_at: str | None = None,
    ) -> int:
        """Create or update a processed email record."""
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO processed_emails (
                gmail_message_id, opportunity_id, subject, sender,
                received_at, filter_score, filter_decision,
                processed_status, error_message, email_classification,
                retry_count, last_retry_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                opportunity_id = excluded.opportunity_id,
                subject = excluded.subject,
                sender = excluded.sender,
                received_at = excluded.received_at,
                filter_score = excluded.filter_score,
                filter_decision = excluded.filter_decision,
                processed_status = excluded.processed_status,
                error_message = excluded.error_message,
                email_classification = excluded.email_classification,
                retry_count = COALESCE(excluded.retry_count, processed_emails.retry_count),
                last_retry_at = COALESCE(excluded.last_retry_at, processed_emails.last_retry_at),
                updated_at = excluded.updated_at;
            """,
            (
                gmail_message_id,
                opportunity_id,
                subject,
                sender,
                received_at,
                filter_score,
                _serialize_value(filter_decision),
                processed_status,
                error_message,
                email_classification,
                retry_count,
                last_retry_at,
                now,
                now,
            ),
        )
        self.connection.commit()

        row = self.connection.execute(
            "SELECT id FROM processed_emails WHERE gmail_message_id = ?;",
            (gmail_message_id,),
        ).fetchone()
        return int(row["id"] if row else cursor.lastrowid)

    def create_notification(
        self,
        *,
        channel: str,
        message: str,
        opportunity_id: int | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        status: str = "pending",
        error_message: str | None = None,
        sent_at: str | None = None,
    ) -> int:
        """Create one notification tracking row."""
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO notifications (
                opportunity_id, channel, recipient, subject,
                message, status, error_message, sent_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                opportunity_id, channel, recipient, subject,
                message, status, error_message, sent_at,
                now, now,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def get_company_history(self) -> list[dict[str, Any]]:
        """Fetch all company records."""
        rows = self.connection.execute(
            "SELECT * FROM companies ORDER BY last_activity DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _insert_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None,
        source_thread_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        company_name = opportunity["company_name"]

        drive_id = generate_drive_id(
            company_name,
            role=opportunity.get("role"),
            category=opportunity.get("internship_or_fulltime"),
            year=_get_year_from_opportunity(opportunity)
        )

        target_hash = generate_unique_hash(opportunity)
        existing_id = self.connection.execute(
            "SELECT id FROM opportunities WHERE unique_hash = ? LIMIT 1", (target_hash,)
        ).fetchone()

        if existing_id:
            logger.warning(
                "[DB] Hash collision detected existing_id=%s incoming_id=NEW",
                existing_id[0],
            )
            return existing_id[0]

        values = {
            **opportunity,
            "unique_hash": target_hash,
            "source_email_id": source_email_id,
            "source_thread_id": source_thread_id,
            "drive_id": drive_id,
            "created_at": now,
            "updated_at": now,
        }
        cursor = self.connection.execute(
            """
            INSERT INTO opportunities (
                unique_hash, company_name, role, internship_or_fulltime,
                package_or_stipend, eligibility, cgpa_requirement,
                branches_allowed, deadline, interview_date, oa_date,
                registration_link, work_location, hiring_process,
                important_notes, source_email_id, created_at, updated_at,
                current_status, status_history, last_update_timestamp,
                email_received_at, drive_id, source_thread_id,
                action_required, email_classification, my_status,
                next_event_date, eligibility_status, applied_date, priority,
                degree_level, dream_category, validation_flags, drive_kind
            )
            VALUES (
                :unique_hash, :company_name, :role, :internship_or_fulltime,
                :package_or_stipend, :eligibility, :cgpa_requirement,
                :branches_allowed, :deadline, :interview_date, :oa_date,
                :registration_link, :work_location, :hiring_process,
                :important_notes, :source_email_id, :created_at, :updated_at,
                :current_status, :status_history, :last_update_timestamp,
                :email_received_at, :drive_id, :source_thread_id,
                :action_required, :email_classification, :my_status,
                :next_event_date, :eligibility_status, :applied_date, :priority,
                COALESCE(:degree_level, 'UNKNOWN'), COALESCE(:dream_category, 'NORMAL'),
                :validation_flags, COALESCE(:drive_kind, 'PLACEMENT')
            );
            """,
            values,
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def _update_opportunity_row(
        self,
        opportunity_id: int,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None,
        source_thread_id: str | None = None,
    ) -> None:
        target_hash = generate_unique_hash(opportunity)
        existing_id = self.connection.execute(
            "SELECT id FROM opportunities WHERE unique_hash = ? LIMIT 1", (target_hash,)
        ).fetchone()

        if existing_id and existing_id[0] != opportunity_id:
            logger.warning(
                "[DB] Hash collision detected existing_id=%s incoming_id=%s",
                existing_id[0], opportunity_id,
            )
            target_hash = self.connection.execute(
                "SELECT unique_hash FROM opportunities WHERE id = ?", (opportunity_id,)
            ).fetchone()[0]

        values = {
            **opportunity,
            "id": opportunity_id,
            "unique_hash": target_hash,
            "source_email_id": source_email_id,
            "source_thread_id": source_thread_id,
            "updated_at": utc_now_iso(),
        }
        self.connection.execute(
            """
            UPDATE opportunities
            SET unique_hash = :unique_hash,
                company_name = :company_name,
                role = :role,
                internship_or_fulltime = :internship_or_fulltime,
                package_or_stipend = :package_or_stipend,
                eligibility = :eligibility,
                cgpa_requirement = :cgpa_requirement,
                branches_allowed = :branches_allowed,
                deadline = COALESCE(:deadline, deadline),
                interview_date = COALESCE(:interview_date, interview_date),
                oa_date = COALESCE(:oa_date, oa_date),
                registration_link = :registration_link,
                work_location = :work_location,
                hiring_process = :hiring_process,
                important_notes = :important_notes,
                source_email_id = COALESCE(:source_email_id, source_email_id),
                updated_at = :updated_at,
                current_status = COALESCE(:current_status, current_status),
                status_history = COALESCE(:status_history, status_history),
                last_update_timestamp = COALESCE(:last_update_timestamp, last_update_timestamp),
                email_received_at = COALESCE(:email_received_at, email_received_at),
                source_thread_id = COALESCE(:source_thread_id, source_thread_id),
                action_required = COALESCE(:action_required, action_required),
                email_classification = COALESCE(:email_classification, email_classification),
                next_event_date = COALESCE(:next_event_date, next_event_date),
                eligibility_status = COALESCE(:eligibility_status, eligibility_status),
                applied_date = COALESCE(:applied_date, applied_date),
                priority = COALESCE(:priority, priority),
                degree_level = COALESCE(:degree_level, degree_level),
                dream_category = COALESCE(:dream_category, dream_category),
                validation_flags = :validation_flags,
                drive_kind = COALESCE(:drive_kind, drive_kind)
            WHERE id = :id;
            """,
            values,
        )
        self.connection.commit()

    def _migrate_existing_opportunities_table(self) -> None:
        """Add required columns when an older local SQLite file already exists."""
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(opportunities);").fetchall()
        }

        new_columns = {
            "current_status": "TEXT NOT NULL DEFAULT 'OPEN'",
            "status_history": "TEXT NOT NULL DEFAULT '[]'",
            "last_update_timestamp": "TEXT",
            "email_received_at": "TEXT",
            "drive_id": "TEXT",
            "source_thread_id": "TEXT",
            "action_required": "TEXT",
            "email_classification": "TEXT",
            "my_status": "TEXT NOT NULL DEFAULT 'NOT_APPLIED'",
            "next_event_date": "TEXT",
            "eligibility_status": "TEXT NOT NULL DEFAULT 'MANUAL_REVIEW'",
            "applied_date": "TEXT",
            "priority": "TEXT NOT NULL DEFAULT 'MEDIUM'",
            "degree_level": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "dream_category": "TEXT NOT NULL DEFAULT 'NORMAL'",
            "validation_flags": "TEXT NOT NULL DEFAULT '[]'",
            "drive_kind": "TEXT NOT NULL DEFAULT 'PLACEMENT'",
        }

        for col_name, col_def in new_columns.items():
            if col_name not in columns:
                logger.info("Migrating opportunities table: adding %s", col_name)
                try:
                    self.connection.execute(
                        f"ALTER TABLE opportunities ADD COLUMN {col_name} {col_def};"
                    )
                except sqlite3.OperationalError:
                    pass  # Column might already exist from another path

        # Ensure companies table
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                total_drives INTEGER NOT NULL DEFAULT 0,
                active_drives INTEGER NOT NULL DEFAULT 0,
                selected_drives INTEGER NOT NULL DEFAULT 0,
                rejected_drives INTEGER NOT NULL DEFAULT 0,
                last_activity TEXT
            );
            """
        )

        # Add email_classification to processed_emails if missing
        pe_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(processed_emails);").fetchall()
        }
        if "email_classification" not in pe_columns:
            try:
                self.connection.execute(
                    "ALTER TABLE processed_emails ADD COLUMN email_classification TEXT;"
                )
            except sqlite3.OperationalError:
                pass
        if "retry_count" not in pe_columns:
            try:
                self.connection.execute(
                    "ALTER TABLE processed_emails "
                    "ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;"
                )
            except sqlite3.OperationalError:
                pass
        if "last_retry_at" not in pe_columns:
            try:
                self.connection.execute(
                    "ALTER TABLE processed_emails ADD COLUMN last_retry_at TEXT;"
                )
            except sqlite3.OperationalError:
                pass

        # Safety-nets plan Phase 1: unmatched_confirmations had no way to be
        # emptied -- add resolved/resolved_at so a row can be marked handled
        # (auto-resolved when a later mail attaches the same
        # gmail_message_id to a drive) and dropped from the digest.
        uc_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(unmatched_confirmations);"
            ).fetchall()
        }
        if "resolved" not in uc_columns:
            try:
                self.connection.execute(
                    "ALTER TABLE unmatched_confirmations "
                    "ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0;"
                )
            except sqlite3.OperationalError:
                pass
        if "resolved_at" not in uc_columns:
            try:
                self.connection.execute(
                    "ALTER TABLE unmatched_confirmations ADD COLUMN resolved_at TEXT;"
                )
            except sqlite3.OperationalError:
                pass

        self.connection.commit()

    def _normalize_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        company_val = opportunity.get("company_name")
        if _normalize_scalar(company_val) is None:
            company_val = "Unknown Company"
        company_name = normalize_company_name(company_val)
        if company_name is None:
            # normalize_company_name rejects junk tokens (e.g. "Location",
            # harvested from "@Own Location") and returns None even for a
            # truthy input -- company_name is NOT NULL, so falling through
            # would crash the insert. Route to the same "couldn't identify
            # a company" sentinel used for a blank value; callers that care
            # about distinguishing "junk extraction" from "genuinely blank"
            # should check email_classification/attachments upstream and
            # divert to unmatched_confirmations before reaching here.
            company_name = "Unknown Company"

        role_val = opportunity.get("role")
        if _normalize_scalar(role_val) is None:
            role_val = "Unknown Role"
        role = _clean_required_text(role_val, "role")

        normalized: dict[str, Any] = {"company_name": company_name, "role": role}

        for fld in OPPORTUNITY_FIELDS + (
            "current_status", "status_history", "last_update_timestamp",
            "email_received_at", "action_required", "email_classification",
            "my_status", "next_event_date", "eligibility_status",
            "applied_date", "priority", "degree_level", "dream_category",
            "validation_flags", "drive_kind",
        ):
            if fld in {"company_name", "role"}:
                continue
            value = opportunity.get(fld)
            if fld in JSON_FIELDS:
                normalized[fld] = _serialize_value(_normalize_list(value))
            elif fld == "status_history":
                normalized[fld] = value if value else "[]"
            elif fld == "my_status":
                normalized[fld] = value if value else "NOT_APPLIED"
            elif fld == "eligibility_status":
                normalized[fld] = value if value else "MANUAL_REVIEW"
            elif fld == "priority":
                normalized[fld] = value if value else "MEDIUM"
            elif fld == "degree_level":
                # None means UNKNOWN — SQL COALESCE preserves a better existing value.
                normalized[fld] = value if value and value != "UNKNOWN" else None
            elif fld == "dream_category":
                # None means NORMAL — SQL COALESCE preserves a better existing value.
                normalized[fld] = value if value and value != "NORMAL" else None
            elif fld == "drive_kind":
                # None means PLACEMENT — SQL COALESCE preserves a non-PLACEMENT
                # classification already on the row (e.g. a follow-up mail for
                # an already-tagged HACKATHON that doesn't repeat the keyword
                # must not silently reclassify it back to PLACEMENT).
                normalized[fld] = value if value and value != "PLACEMENT" else None
            else:
                normalized[fld] = _normalize_scalar(value)

        return normalized

    def _changed_fields(
        self,
        existing: sqlite3.Row,
        incoming: dict[str, Any],
    ) -> list[tuple[str, Any, Any]]:
        return self._changed_fields_from_dict(dict(existing), incoming)

    def _changed_fields_from_dict(
        self,
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> list[tuple[str, Any, Any]]:
        changes: list[tuple[str, Any, Any]] = []
        for fld in OPPORTUNITY_FIELDS:
            if fld in {"company_name", "role"}:
                continue
            old_value = existing.get(fld)
            if fld in JSON_FIELDS and isinstance(old_value, list):
                old_value = _serialize_value(old_value)
            new_value = incoming.get(fld)
            if old_value != new_value and new_value is not None:
                changes.append((fld, old_value, new_value))

        # Also track status changes
        if existing.get("current_status") != incoming.get("current_status"):
            changes.append(
                (
                    "current_status",
                    existing.get("current_status"),
                    incoming.get("current_status"),
                )
            )

        return changes

    def _row_to_opportunity(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for fld in JSON_FIELDS:
            data[fld] = _deserialize_json_list(data.get(fld))
        return data

    def _update_company_stats(self, company_name: str) -> None:
        """Update the companies table metrics."""
        normalized = normalize_company_name(company_name)
        now = utc_now_iso()

        self.connection.execute(
            "INSERT OR IGNORE INTO companies (name) VALUES (?)", (normalized,)
        )

        total = self.connection.execute(
            "SELECT COUNT(*) FROM opportunities WHERE company_name = ?", (normalized,)
        ).fetchone()[0]
        active = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM opportunities
            WHERE company_name = ?
              AND current_status NOT IN (
                  'REJECTED', 'OFFER_RECEIVED', 'EXPIRED', 'WITHDRAWN'
              )
            """,
            (normalized,),
        ).fetchone()[0]
        selected = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM opportunities
            WHERE company_name = ?
              AND current_status IN ('SELECTED', 'OFFER_RECEIVED')
            """,
            (normalized,),
        ).fetchone()[0]
        rejected = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM opportunities
            WHERE company_name = ?
              AND current_status = 'REJECTED'
            """,
            (normalized,),
        ).fetchone()[0]

        self.connection.execute(
            """
            UPDATE companies
            SET total_drives = ?, active_drives = ?,
                selected_drives = ?, rejected_drives = ?,
                last_activity = ?
            WHERE name = ?;
            """,
            (total, active, selected, rejected, now, normalized),
        )
        self.connection.commit()


# ------------------------------------------------------------------
# Module-level Helpers
# ------------------------------------------------------------------


def _within_next_days(date_str: str | None, days: int) -> bool:
    """Return True when ``date_str`` parses to a date within the next ``days``.

    Today and future dates up to the horizon count; past dates do not. Dates are
    parsed flexibly because extracted values are rarely ISO formatted.
    """
    if not date_str:
        return False
    parsed = parse_datetime_flexible(str(date_str))
    if parsed is None:
        return False
    now = datetime.now()
    return now.date() <= parsed.date() <= (now + timedelta(days=days)).date()


def _normalize_key(value: str | None) -> str:
    """Normalize identity fields before hashing."""
    if not value:
        return ""
    return " ".join(value.casefold().strip().split())


def _clean_required_text(value: Any, field_name: str) -> str:
    normalized = _normalize_scalar(value)
    if normalized is None:
        msg = f"{field_name} is required for opportunity storage."
        raise ValueError(msg)
    return normalized


def _normalize_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = ", ".join(str(item).strip() for item in value if str(item).strip())
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
        return None
    return normalized


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) and value.strip().startswith("["):
        # Already-JSON-serialized input (e.g. update_opportunity re-normalizing
        # a dict insert_or_update_opportunity already normalized) — parse it
        # back into a list instead of comma-splitting the JSON syntax itself.
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
        return []
    return [item.strip() for item in normalized.replace(";", ",").split(",") if item.strip()]


def _serialize_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return str(value)


def _deserialize_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if not isinstance(loaded, list):
        return [str(loaded)]
    return [str(item) for item in loaded]
