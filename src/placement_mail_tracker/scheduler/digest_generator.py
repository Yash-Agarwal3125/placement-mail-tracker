"""Daily Digest generator for Placement Mail Tracker."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.notifications.email_notifier import EmailNotifier
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

        if not (action_required or upcoming_events or new_opps):
            logger.info("No significant updates for the daily digest.")
            self._record_digest_sent()
            return False

        digest_body = _format_digest(action_required, upcoming_events, new_opps, now)

        subject = f"Placement Summary - {now.strftime('%d %b %Y')}"
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


def _format_digest(
    action_required: list[dict[str, Any]],
    upcoming_events: list[dict[str, Any]],
    new_opps: list[dict[str, Any]],
    now: datetime,
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

    return "\n".join(lines)


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
