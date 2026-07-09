"""One-shot "OAuth dead — re-consent needed" alerts, deduped per service.

ADR-D8 / Decision 4: a dead refresh token today surfaces (at best) as a vague
generic "failure streak: N" email after several consecutive failed runs. This
sends one clear, immediate SMTP alert naming the exact service the first time
its token dies, then stays silent on every subsequent run until the token is
re-consented (``clear_oauth_alert``) — no repeat spam every 3 hours.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.notifications.email_notifier import EmailNotifier

logger = logging.getLogger(__name__)

_STATE_FILE = Path("data/oauth_alert_state.json")


def _read_state() -> dict[str, bool]:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_state(state: dict[str, bool]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as error:
        logger.warning("Could not persist OAuth alert state: %s", error)


def alert_oauth_dead_once(service_name: str, message: str, settings: Settings) -> None:
    """Send one SMTP alert for a dead OAuth token; a no-op if already sent."""
    state = _read_state()
    if state.get(service_name):
        return
    notifier = EmailNotifier(settings)
    sent = notifier.send_email(
        f"Placement Mail Tracker: {service_name} OAuth dead — re-consent needed",
        f"{message}\n\nRun the tracker interactively once to complete the OAuth "
        f"re-consent flow for {service_name}.",
    )
    if sent:
        state[service_name] = True
        _write_state(state)


def clear_oauth_alert(service_name: str) -> None:
    """Clear the dedup flag once a service authenticates successfully again."""
    state = _read_state()
    if state.pop(service_name, None) is not None:
        _write_state(state)
