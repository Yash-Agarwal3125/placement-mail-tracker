"""Daily Digest generator for Placement Mail Tracker."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.notifications.email_notifier import EmailNotifier
from placement_mail_tracker.scheduler.calendar_flags_store import pop_pending_calendar_flags
from placement_mail_tracker.utils.time import parse_datetime_flexible

logger = logging.getLogger(__name__)


class DailyDigestGenerator:
    """Generates and sends a daily plain-text summary of placement activities."""

    def __init__(self, database: DatabaseManager, settings: Settings):
        self.database = database
        self.settings = settings
        self.notifier = EmailNotifier(settings)

    def generate_and_send(self) -> bool:
        """Generate and email the daily digest when it is due."""
        now = datetime.now()
        yesterday_iso = (now - timedelta(days=1)).isoformat()

        send_hour, send_minute = map(int, self.settings.digest_send_time.split(":"))
        if now.hour < send_hour or (now.hour == send_hour and now.minute < send_minute):
            logger.debug("Too early for daily digest. Waiting for %s", self.settings.digest_send_time)  # noqa: E501
            return False

        if self._digest_already_sent_today(now):
            logger.info("Daily digest already sent today. Skipping.")
            return False

        opportunities = self.database.fetch_active_opportunities()

        action_required: list[dict[str, Any]] = []
        upcoming_events: list[dict[str, Any]] = []
        new_opps: list[dict[str, Any]] = []

        now_date = now.date()

        for opp in opportunities:
            if "NOT_ELIGIBLE" in (opp.get("eligibility_status") or ""):
                continue

            created_at = opp.get("created_at", "")
            if created_at > yesterday_iso:
                new_opps.append(opp)

            status = (opp.get("current_status") or "").upper()
            if status in ("OPEN", "REGISTERED"):
                action_required.append(opp)

            for date_field in ("interview_date", "oa_date", "next_event_date"):
                raw = opp.get(date_field)
                if not raw:
                    continue
                parsed = parse_datetime_flexible(str(raw))
                if parsed and 0 <= (parsed.date() - now_date).days <= 7:
                    upcoming_events.append(opp)
                    break

        dead_letter_count = self.database.get_dead_letter_count()
        calendar_flags = pop_pending_calendar_flags()

        if not (
            action_required or upcoming_events or new_opps or dead_letter_count or calendar_flags
        ):
            logger.info("No significant updates for the daily digest.")
            self._record_digest_sent()
            return False

        digest_body = _format_digest(
            action_required, upcoming_events, new_opps, now, dead_letter_count, calendar_flags
        )

        # Build a descriptive subject line
        subject = _build_subject(action_required, new_opps, upcoming_events, now)
        logger.info("Sending Daily Digest via email.")
        success = self.notifier.send_email(subject=subject, body=digest_body, is_html=False)

        if success:
            self._record_digest_sent()

        return success

    def _digest_already_sent_today(self, now: datetime) -> bool:
        today_str = now.strftime("%Y-%m-%d")
        row = self.database.connection.execute(
            """
            SELECT id FROM notifications
            WHERE channel = 'digest'
              AND status = 'sent'
              AND date(created_at) = ?
            """,
            (today_str,),
        ).fetchone()
        return row is not None

    def _record_digest_sent(self) -> None:
        self.database.create_notification(
            channel="digest",
            message="Daily digest execution",
            status="sent",
        )


def _build_subject(
    action_required: list[dict[str, Any]],
    new_opps: list[dict[str, Any]],
    upcoming_events: list[dict[str, Any]],
    now: datetime,
) -> str:
    parts = []
    deadlines_today = sum(
        1 for o in action_required
        if _deadline_delta(o.get("deadline"), now) == 0
    )
    oa_tomorrow = sum(
        1 for o in upcoming_events
        if _event_delta(o, now) == 1
    )
    if deadlines_today:
        n = deadlines_today
        parts.append(f"🚨 {n} Deadline{'s' if n > 1 else ''} Today")
    if new_opps:
        n = len(new_opps)
        parts.append(f"{n} New Drive{'s' if n > 1 else ''}")
    if oa_tomorrow:
        n = oa_tomorrow
        parts.append(f"{n} OA Tomorrow")
    return " | ".join(parts) if parts else f"Placement Summary - {now.strftime('%d %b %Y')}"


def _format_digest(
    action_required: list[dict[str, Any]],
    upcoming_events: list[dict[str, Any]],
    new_opps: list[dict[str, Any]],
    now: datetime,
    dead_letter_count: int = 0,
    calendar_flags: list[str] | None = None,
) -> str:
    lines = ["PLACEMENT SUMMARY", ""]

    if action_required:
        lines.append("ACTION REQUIRED")
        for opp in action_required:
            company = opp.get("company_name") or "?"
            hint = _deadline_hint(opp.get("deadline"), now)
            lines.append(f"* {company}{hint}")
        lines.append("")

    if upcoming_events:
        lines.append("UPCOMING INTERVIEWS / ASSESSMENTS")
        for opp in upcoming_events:
            company = opp.get("company_name") or "?"
            for date_field in ("interview_date", "oa_date", "next_event_date"):
                raw = opp.get(date_field)
                if raw:
                    parsed = parse_datetime_flexible(str(raw))
                    if parsed:
                        date_str = f"{parsed.day} {parsed.strftime('%b')}"
                        lines.append(f"* {company} - {date_str}")
                        break
            else:
                lines.append(f"* {company}")
        lines.append("")

    if new_opps:
        lines.append("NEW OPPORTUNITIES")
        for opp in new_opps:
            lines.append(f"* {opp.get('company_name') or '?'}")
        lines.append("")

    if dead_letter_count:
        lines.append("SYSTEM HEALTH")
        lines.append(f"* {dead_letter_count} email(s) failed processing permanently (dead letters)")
        lines.append("  Check logs or the RECENT UPDATES sheet for details.")
        lines.append("")

    if calendar_flags:
        lines.append("CALENDAR FLAGS")
        for flag in calendar_flags:
            lines.append(f"* {flag}")
        lines.append("")

    return "\n".join(lines)


def _deadline_delta(deadline_raw: str | None, now: datetime) -> int | None:
    if not deadline_raw:
        return None
    parsed = parse_datetime_flexible(str(deadline_raw))
    if not parsed:
        return None
    return (parsed.date() - now.date()).days


def _event_delta(opp: dict[str, Any], now: datetime) -> int | None:
    for field in ("interview_date", "oa_date", "next_event_date"):
        raw = opp.get(field)
        if raw:
            parsed = parse_datetime_flexible(str(raw))
            if parsed:
                return (parsed.date() - now.date()).days
    return None


def _deadline_hint(deadline_raw: str | None, now: datetime) -> str:
    if not deadline_raw:
        return ""
    parsed = parse_datetime_flexible(str(deadline_raw))
    if not parsed:
        return ""
    days = (parsed.date() - now.date()).days
    if days == 0:
        return " - Apply Today"
    if days == 1:
        return " - Apply Tomorrow"
    if days < 0:
        return " - Deadline Passed"
    return f" - {days} days left"
