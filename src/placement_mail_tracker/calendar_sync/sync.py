"""Calendar diff engine (docs/design/04-integration-spec.md §3.3).

Drives the whole calendar sync pass: derive desired events (``derive.py``),
diff them against the stored ``calendar_events`` state (via
``DatabaseManager``), and call the Google Calendar client (``client.py``) to
insert/PATCH as needed. Never deletes a Google event or a state row — vanished
or terminal drives get retitled (``[?] ...``) instead (ADR Decision 1/2/7).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from placement_mail_tracker.calendar_sync.client import (
    CalendarAuthenticationError,
    GoogleCalendarClient,
)
from placement_mail_tracker.calendar_sync.derive import CalendarEvent, derive_events
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

# current_status values that mean "this drive is over" (manager.py VALID_STATUSES
# tail); used by the stale pass to decide cancelled vs stale (ADR D2 table).
_TERMINAL_STATUSES = frozenset({"REJECTED", "WITHDRAWN", "EXPIRED", "COMPLETED"})


@dataclass(slots=True)
class CalendarSyncResult:
    """Outcome of one ``CalendarSyncEngine.sync()``/``rebuild()`` pass."""

    inserted: int = 0
    patched: int = 0
    unchanged: int = 0
    marked_done: int = 0
    retitled_stale: int = 0
    flagged: list[str] = field(default_factory=list)
    dry_run: bool = False


class CalendarSyncEngine:
    """Diffs derived calendar events against stored state and syncs Google Calendar."""

    def __init__(
        self,
        database: DatabaseManager,
        client: GoogleCalendarClient,
        settings: Settings,
    ) -> None:
        self.database = database
        self.client = client
        self.settings = settings
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def sync(self, *, dry_run: bool = False) -> CalendarSyncResult:
        """One diff pass over active drives (spec §3.3, ADR Decision 2's table)."""
        result = CalendarSyncResult(dry_run=dry_run)
        self.last_error = None

        rows = self.database.fetch_active_drives_only()
        # Partial-fetch guard (ADR D1/D2): an empty active-drive fetch must
        # never look like "every drive vanished" — skip the stale pass, but
        # still derive/diff whatever the (empty) row set produces.
        stale_pass_enabled = bool(rows)

        desired, anomalies = derive_events(rows, self.settings)
        result.flagged.extend(anomalies)

        calendar_id: str | None = None
        if not dry_run:
            calendar_id = self.client.ensure_calendar(self.settings.calendar_name)

        states_by_key = self._load_states()
        now = datetime.now(ZoneInfo(self.settings.calendar_timezone))

        desired_keys: set[tuple[int, str]] = set()
        for event in desired:
            key = (event.opportunity_id, event.event_type)
            desired_keys.add(key)
            existing = states_by_key.get(key)

            if existing is None:
                self._handle_insert(event, calendar_id, dry_run, result, states_by_key)
                continue

            if existing["status"] == "done":
                continue  # frozen — excluded from all future diffing

            # A drive that was previously retitled `[?] ...` by the stale pass
            # (status 'stale'/'cancelled') and has now reappeared as active
            # must have its Google title restored even when the underlying
            # dates/location are unchanged — the stored content_hash predates
            # the `[?] ` retitle (set_calendar_event_status only touches
            # status), so a pure hash-equality check would wrongly call this
            # "unchanged" and leave the stale-looking title on Google forever.
            reactivating = existing["status"] != "active"
            if not reactivating and existing["content_hash"] == event.content_hash():
                self._handle_unchanged(event, existing, calendar_id, dry_run, result, states_by_key)
            else:
                self._handle_patch(event, existing, calendar_id, dry_run, result, states_by_key)

        self._done_pass(states_by_key, now, dry_run, result)

        active_opp_ids = {row["id"] for row in rows}
        self._null_date_pass(states_by_key, active_opp_ids, desired_keys, result)

        if stale_pass_enabled:
            self._stale_pass(states_by_key, active_opp_ids, calendar_id, dry_run, result)

        return result

    def rebuild(self) -> CalendarSyncResult:
        """Reconciliation, not recreation (ADR Decision 7).

        For every non-``done`` ``calendar_events`` row: GET the stored
        ``gcal_event_id``; missing (``None``) -> re-insert and store the new
        id; present but drifted from our record -> PATCH back to match.
        Never deletes rows or Google events.
        """
        result = CalendarSyncResult(dry_run=False)
        self.last_error = None

        for state in self.database.fetch_calendar_event_states():
            # 'done' rows are frozen by the normal sync diff too — a finished
            # event has no reason to be reconciled. Judgment call: spec says
            # "every calendar_events row"; excluding done keeps rebuild's
            # semantics symmetric with the diff table.
            if state["status"] == "done":
                continue

            calendar_id = state.get("gcal_calendar_id")
            if not calendar_id:
                # Never made it past an insert attempt — nothing to reconcile.
                continue

            gcal_event_id = state.get("gcal_event_id")
            remote: dict[str, Any] | None = None
            if gcal_event_id:
                try:
                    remote = self.client.get_event(calendar_id, gcal_event_id)
                except CalendarAuthenticationError:
                    raise
                except Exception as error:  # noqa: BLE001 - per-row isolation
                    logger.warning(
                        "Calendar rebuild get_event failed for row id=%s: %s",
                        state.get("id"), error,
                    )
                    self.last_error = str(error)
                    continue

            body = self._body_from_state(state)

            if remote is None:
                try:
                    new_gcal_event_id = self.client.insert_event(calendar_id, body)
                except CalendarAuthenticationError:
                    raise
                except Exception as error:  # noqa: BLE001 - per-row isolation
                    logger.warning(
                        "Calendar rebuild insert failed for row id=%s: %s",
                        state.get("id"), error,
                    )
                    self.last_error = str(error)
                    continue
                event = self._calendar_event_from_state(state)
                self.database.upsert_calendar_event_state(
                    event,
                    gcal_calendar_id=calendar_id,
                    gcal_event_id=new_gcal_event_id,
                    status=state["status"],
                )
                result.inserted += 1
                continue

            if self._remote_matches_state(remote, state):
                result.unchanged += 1
                continue

            try:
                self.client.patch_event(calendar_id, gcal_event_id, body)
            except CalendarAuthenticationError:
                raise
            except Exception as error:  # noqa: BLE001 - per-row isolation
                logger.warning(
                    "Calendar rebuild patch failed for row id=%s: %s",
                    state.get("id"), error,
                )
                self.last_error = str(error)
                continue
            result.patched += 1

        return result

    # ------------------------------------------------------------------
    # sync() step helpers
    # ------------------------------------------------------------------

    def _load_states(self) -> dict[tuple[int, str], dict[str, Any]]:
        states = self.database.fetch_calendar_event_states()
        return {(s["opportunity_id"], s["event_type"]): dict(s) for s in states}

    def _handle_insert(
        self,
        event: CalendarEvent,
        calendar_id: str | None,
        dry_run: bool,
        result: CalendarSyncResult,
        states_by_key: dict[tuple[int, str], dict[str, Any]],
    ) -> None:
        key = (event.opportunity_id, event.event_type)
        if dry_run:
            logger.info(
                "PLAN: insert %s %s for opportunity_id=%s",
                event.event_type, event.title, event.opportunity_id,
            )
            result.inserted += 1
            states_by_key[key] = self._synthetic_state(
                event, gcal_event_id=None, status="active", row_id=None
            )
            return

        try:
            gcal_event_id = self.client.insert_event(calendar_id, self._build_body(event))
        except CalendarAuthenticationError:
            raise
        except Exception as error:  # noqa: BLE001 - one bad event must not abort the pass
            logger.warning(
                "Calendar insert failed for %s %s (opportunity_id=%s): %s",
                event.event_type, event.title, event.opportunity_id, error,
            )
            self.last_error = str(error)
            return

        # State is written only after the API call succeeds (ADR D2 crash-safety).
        row_id = self.database.upsert_calendar_event_state(
            event, gcal_calendar_id=calendar_id, gcal_event_id=gcal_event_id, status="active"
        )
        result.inserted += 1
        states_by_key[key] = self._synthetic_state(
            event, gcal_event_id=gcal_event_id, status="active", row_id=row_id
        )

    def _handle_unchanged(
        self,
        event: CalendarEvent,
        existing: dict[str, Any],
        calendar_id: str | None,
        dry_run: bool,
        result: CalendarSyncResult,
        states_by_key: dict[tuple[int, str], dict[str, Any]],
    ) -> None:
        result.unchanged += 1
        key = (event.opportunity_id, event.event_type)
        if dry_run:
            logger.info(
                "PLAN: unchanged %s %s for opportunity_id=%s",
                event.event_type, event.title, event.opportunity_id,
            )
            return

        # No API call — just bump last_seen_active_at (every matched row does).
        row_id = self.database.upsert_calendar_event_state(
            event,
            gcal_calendar_id=calendar_id,
            gcal_event_id=existing["gcal_event_id"],
            status="active",
        )
        states_by_key[key] = self._synthetic_state(
            event, gcal_event_id=existing["gcal_event_id"], status="active", row_id=row_id
        )

    def _handle_patch(
        self,
        event: CalendarEvent,
        existing: dict[str, Any],
        calendar_id: str | None,
        dry_run: bool,
        result: CalendarSyncResult,
        states_by_key: dict[tuple[int, str], dict[str, Any]],
    ) -> None:
        key = (event.opportunity_id, event.event_type)
        if dry_run:
            logger.info(
                "PLAN: patch %s %s for opportunity_id=%s",
                event.event_type, event.title, event.opportunity_id,
            )
            result.patched += 1
            states_by_key[key] = self._synthetic_state(
                event,
                gcal_event_id=existing["gcal_event_id"],
                status="active",
                row_id=existing.get("id"),
            )
            return

        gcal_event_id = existing["gcal_event_id"]
        try:
            self.client.patch_event(calendar_id, gcal_event_id, self._build_body(event))
        except CalendarAuthenticationError:
            raise
        except Exception as error:  # noqa: BLE001 - one bad event must not abort the pass
            logger.warning(
                "Calendar patch failed for %s %s (opportunity_id=%s): %s",
                event.event_type, event.title, event.opportunity_id, error,
            )
            self.last_error = str(error)
            return

        row_id = self.database.upsert_calendar_event_state(
            event, gcal_calendar_id=calendar_id, gcal_event_id=gcal_event_id, status="active"
        )
        result.patched += 1
        states_by_key[key] = self._synthetic_state(
            event, gcal_event_id=gcal_event_id, status="active", row_id=row_id
        )

    def _done_pass(
        self,
        states_by_key: dict[tuple[int, str], dict[str, Any]],
        now: datetime,
        dry_run: bool,
        result: CalendarSyncResult,
    ) -> None:
        for state in states_by_key.values():
            if state["status"] != "active":
                continue
            end_dt = self._effective_end(state["end_iso"], bool(state["all_day"]))
            if end_dt is None or end_dt >= now:
                continue

            if dry_run:
                logger.info(
                    "PLAN: mark done %s for opportunity_id=%s",
                    state["event_type"], state["opportunity_id"],
                )
            elif state.get("id") is not None:
                self.database.set_calendar_event_status(state["id"], "done")

            state["status"] = "done"
            result.marked_done += 1

    def _null_date_pass(
        self,
        states_by_key: dict[tuple[int, str], dict[str, Any]],
        active_opp_ids: set[int],
        desired_keys: set[tuple[int, str]],
        result: CalendarSyncResult,
    ) -> None:
        for key, state in states_by_key.items():
            if state["status"] != "active":
                continue
            opportunity_id, event_type = key
            if opportunity_id not in active_opp_ids:
                continue  # vanished drives are the stale pass's concern
            if key in desired_keys:
                continue  # still has a date this pass

            label = state.get("drive_id") or opportunity_id
            result.flagged.append(
                f"{label}: {event_type} date became empty — calendar event kept as-is"
            )

    def _stale_pass(
        self,
        states_by_key: dict[tuple[int, str], dict[str, Any]],
        active_opp_ids: set[int],
        calendar_id: str | None,
        dry_run: bool,
        result: CalendarSyncResult,
    ) -> None:
        stale_after = timedelta(hours=self.settings.calendar_stale_after_hours)
        reference = datetime.now(timezone.utc)

        for key, state in states_by_key.items():
            if state["status"] != "active":
                continue
            opportunity_id, _event_type = key
            if opportunity_id in active_opp_ids:
                continue  # still visible — not a candidate

            last_seen = self._parse_utc(state.get("last_seen_active_at"))
            if last_seen is None or reference - last_seen < stale_after:
                continue  # inside the grace period (or no timestamp to judge by)

            opportunity = self.database.fetch_opportunity_by_id(opportunity_id)
            current_status = ""
            if opportunity:
                current_status = (opportunity.get("current_status") or "").upper()
            is_terminal = current_status in _TERMINAL_STATUSES
            new_status = "cancelled" if is_terminal else "stale"
            new_title = f"[?] {state['title']}"

            if dry_run:
                logger.info(
                    "PLAN: retitle stale '%s' -> '%s' (opportunity_id=%s, status=%s)",
                    state["title"], new_title, opportunity_id, new_status,
                )
                result.retitled_stale += 1
                state["title"] = new_title
                state["status"] = new_status
                continue

            gcal_event_id = state.get("gcal_event_id")
            if not gcal_event_id:
                continue  # nothing on Google to retitle

            try:
                self.client.patch_event(calendar_id, gcal_event_id, {"summary": new_title})
            except CalendarAuthenticationError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad event must not abort the pass
                logger.warning(
                    "Calendar stale retitle failed for opportunity_id=%s: %s", opportunity_id, error
                )
                self.last_error = str(error)
                continue

            row_id = state.get("id")
            if row_id is not None:
                self.database.set_calendar_event_status(row_id, new_status)
            result.retitled_stale += 1
            state["title"] = new_title
            state["status"] = new_status

    # ------------------------------------------------------------------
    # Small shared helpers
    # ------------------------------------------------------------------

    def _effective_end(self, end_iso: str, all_day: bool) -> datetime | None:
        """Parse a stored end_iso into a tz-aware instant for done-pass comparison.

        All-day events "end" 23:59:59 local on their end date (ADR D3/B7) so the
        done-freeze rule doesn't fire at 00:01 on the event day itself.
        """
        try:
            if all_day:
                date_part = datetime.strptime(end_iso[:10], "%Y-%m-%d")
                return date_part.replace(
                    hour=23, minute=59, second=59, tzinfo=ZoneInfo(self.settings.calendar_timezone)
                )
            return datetime.fromisoformat(end_iso)
        except ValueError:
            return None

    @staticmethod
    def _parse_utc(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _synthetic_state(
        event: CalendarEvent, *, gcal_event_id: str | None, status: str, row_id: int | None
    ) -> dict[str, Any]:
        """Build an in-memory state dict shaped like a ``calendar_events`` row.

        Used to keep later steps of the same ``sync()`` pass (done/null-date/
        stale) consistent with what was just inserted/patched/left unchanged,
        without a second DB round-trip — this also makes dry-run exercise the
        exact same decision logic as a real run (ADR Decision 7).
        """
        return {
            "id": row_id,
            "opportunity_id": event.opportunity_id,
            "drive_id": event.drive_id,
            "event_type": event.event_type,
            "gcal_calendar_id": None,
            "gcal_event_id": gcal_event_id,
            "start_iso": event.start_iso,
            "end_iso": event.end_iso,
            "all_day": int(event.all_day),
            "title": event.title,
            "location": event.location,
            "content_hash": event.content_hash(),
            "status": status,
            "last_seen_active_at": utc_now_iso(),
            "created_at": None,
            "updated_at": None,
        }

    def _build_body(self, event: CalendarEvent) -> dict[str, Any]:
        """Google Calendar event resource dict from a desired ``CalendarEvent``."""
        if event.all_day:
            start: dict[str, str] = {"date": event.start_iso}
            end: dict[str, str] = {"date": self._google_all_day_end(event.end_iso)}
        else:
            start = {"dateTime": event.start_iso, "timeZone": self.settings.calendar_timezone}
            end = {"dateTime": event.end_iso, "timeZone": self.settings.calendar_timezone}

        body: dict[str, Any] = {
            "summary": event.title,
            "description": event.description,
            "start": start,
            "end": end,
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": minutes} for minutes in event.reminder_minutes
                ],
            },
            "extendedProperties": {
                "private": {
                    "drive_id": event.drive_id or "",
                    "opportunity_id": str(event.opportunity_id),
                }
            },
        }
        if event.location:
            body["location"] = event.location
        return body

    def _body_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Google Calendar body reconstructed from a stored ``calendar_events`` row.

        Used only by ``rebuild()``. ``description``/``reminder_minutes`` are not
        persisted in ``calendar_events`` and so cannot be reconstructed here —
        a rebuilt/re-inserted event carries an empty description and default
        reminders until the next normal ``sync()`` pass PATCHes it back from a
        freshly-derived ``CalendarEvent`` (documented limitation).
        """
        all_day = bool(state["all_day"])
        if all_day:
            start: dict[str, str] = {"date": state["start_iso"]}
            end: dict[str, str] = {"date": self._google_all_day_end(state["end_iso"])}
        else:
            start = {"dateTime": state["start_iso"], "timeZone": self.settings.calendar_timezone}
            end = {"dateTime": state["end_iso"], "timeZone": self.settings.calendar_timezone}

        body: dict[str, Any] = {
            "summary": state["title"],
            "start": start,
            "end": end,
            "extendedProperties": {
                "private": {
                    "drive_id": state.get("drive_id") or "",
                    "opportunity_id": str(state["opportunity_id"]),
                }
            },
        }
        if state.get("location"):
            body["location"] = state["location"]
        return body

    @staticmethod
    def _calendar_event_from_state(state: dict[str, Any]) -> CalendarEvent:
        """Reconstruct a minimal ``CalendarEvent`` from a stored row (rebuild only).

        ``description``/``reminder_minutes`` are not columns on ``calendar_events``
        so they can't be restored — a subsequent normal sync PATCHes them back in.
        """
        return CalendarEvent(
            opportunity_id=state["opportunity_id"],
            drive_id=state.get("drive_id"),
            event_type=state["event_type"],
            title=state["title"],
            start_iso=state["start_iso"],
            end_iso=state["end_iso"],
            all_day=bool(state["all_day"]),
            location=state.get("location"),
            description="",
            reminder_minutes=[],
        )

    @staticmethod
    def _google_all_day_end(end_iso: str) -> str:
        """Convert our inclusive same-day ``end_iso`` to Google's EXCLUSIVE
        all-day ``end.date`` (one day after the last day of the event).

        ``CalendarEvent``/``content_hash`` represent an all-day event's last
        INCLUSIVE day (``start_iso == end_iso``); the Calendar API requires
        ``end.date`` to be the day *after* for a single-day all-day event, or
        it rejects the body as an empty/invalid range. Only the wire body
        needs this adjustment — ``_remote_matches_state`` undoes it before
        comparing back against our stored hash.
        """
        end_date = datetime.strptime(end_iso[:10], "%Y-%m-%d") + timedelta(days=1)
        return end_date.date().isoformat()

    @staticmethod
    def _remote_matches_state(remote: dict[str, Any], state: dict[str, Any]) -> bool:
        """Compare a live Google event to our stored record (rebuild only).

        This is a different comparison than the normal sync's hash-vs-hash diff:
        there is no local ``CalendarEvent`` here, only Google's own response, so
        a comparable hash is built from the same four fields
        (start|end|summary|location) read off the remote body.
        """
        start = remote.get("start") or {}
        end = remote.get("end") or {}
        remote_start = start.get("dateTime") or start.get("date") or ""
        remote_end_raw = end.get("dateTime") or end.get("date") or ""
        if "date" in end and remote_end_raw:
            # Undo Google's exclusive all-day end.date so this lines up with
            # the inclusive-end convention CalendarEvent.content_hash() used.
            remote_end_dt = datetime.strptime(remote_end_raw, "%Y-%m-%d") - timedelta(days=1)
            remote_end = remote_end_dt.date().isoformat()
        else:
            remote_end = remote_end_raw
        remote_summary = remote.get("summary") or ""
        remote_location = remote.get("location") or ""
        parts = "|".join([remote_start, remote_end, remote_summary, remote_location])
        remote_hash = hashlib.sha256(parts.encode("utf-8")).hexdigest()
        return remote_hash == state["content_hash"]
