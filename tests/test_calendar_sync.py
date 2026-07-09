"""Tests for the calendar diff engine (docs/design/04-integration-spec.md §5, cases 9-18).

Uses the real in-memory ``db_manager``/``mock_settings``/``sample_opportunity``
fixtures from ``tests/conftest.py`` and a local, dependency-free fake
``GoogleCalendarClient`` so these tests are independent of that module's real
implementation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from placement_mail_tracker.calendar_sync.client import CalendarAuthenticationError
from placement_mail_tracker.calendar_sync.sync import CalendarSyncEngine


class FakeCalendarClient:
    """Minimal stand-in for GoogleCalendarClient (no delete method exists at all)."""

    def __init__(self) -> None:
        self.calendar_id = "cal-vit-placements"
        self.ensure_calendar_calls = 0
        self.insert_calls: list[tuple[str, dict[str, Any]]] = []
        self.patch_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, str]] = []
        self._events: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.last_error: str | None = None

    def ensure_calendar(self, name: str) -> str:
        self.ensure_calendar_calls += 1
        return self.calendar_id

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> str:
        event_id = f"evt-{self._next_id}"
        self._next_id += 1
        self._events[event_id] = dict(body)
        self.insert_calls.append((calendar_id, dict(body)))
        return event_id

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        self.patch_calls.append((calendar_id, event_id, dict(body)))
        self._events.setdefault(event_id, {}).update(body)

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        self.get_calls.append((calendar_id, event_id))
        return self._events.get(event_id)


# ---------------------------------------------------------------------------
# Case 9 — new drive inserts once, extendedProperties carry drive_id + opportunity_id
# ---------------------------------------------------------------------------


def test_new_drive_inserts_once(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17-Aug-2026 05:30 PM")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-9")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)

    result = engine.sync()

    assert result.inserted == 1
    assert client.ensure_calendar_calls == 1
    assert len(client.insert_calls) == 1

    _, body = client.insert_calls[0]
    private_props = body["extendedProperties"]["private"]
    assert private_props["opportunity_id"] == str(opp_id)
    assert "drive_id" in private_props

    states = db_manager.fetch_calendar_event_states()
    assert len(states) == 1
    assert states[0]["status"] == "active"
    assert states[0]["gcal_event_id"] is not None


# ---------------------------------------------------------------------------
# Regression: Google Calendar all-day events need an EXCLUSIVE end.date (one
# day after the last inclusive day) or the API rejects the body as an empty
# range. derive.py's CalendarEvent stores start_iso == end_iso for a single
# all-day day; _build_body must add one day only in the wire body.
# ---------------------------------------------------------------------------


def test_all_day_event_body_has_exclusive_end_date(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17 August 2026")  # date-only -> all-day
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-alldayfix")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)

    engine.sync()

    _, body = client.insert_calls[0]
    assert body["start"]["date"] == "2026-08-17"
    assert body["end"]["date"] == "2026-08-18"


# ---------------------------------------------------------------------------
# Case 10 — second sync with nothing changed makes zero API calls
# ---------------------------------------------------------------------------


def test_second_sync_nothing_changed_is_a_noop(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17-Aug-2026 05:30 PM")
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-10")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    client.insert_calls.clear()
    client.patch_calls.clear()

    result = engine.sync()

    assert result.inserted == 0
    assert result.patched == 0
    assert result.unchanged == 1
    assert client.insert_calls == []
    assert client.patch_calls == []


# ---------------------------------------------------------------------------
# Case 11 — required reschedule simulation: exactly one patch by the stored id
# ---------------------------------------------------------------------------


def test_reschedule_patches_by_stored_event_id(db_manager, mock_settings, sample_opportunity):
    settings = mock_settings.model_copy(update={"calendar_sync_mode": "all_eligible"})
    opp = sample_opportunity(oa_date="10 August 2026")
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-11")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, settings)

    result1 = engine.sync()
    assert result1.inserted == 1
    stored_event_id = db_manager.fetch_calendar_event_states()[0]["gcal_event_id"]

    # Follow-up email on the same thread reschedules the OA date.
    followup = sample_opportunity(oa_date="17 August 2026")
    db_manager.insert_or_update_opportunity(followup, source_thread_id="thread-11")

    client.insert_calls.clear()
    client.patch_calls.clear()
    client.get_calls.clear()

    result2 = engine.sync()

    assert result2.patched == 1
    assert result2.inserted == 0
    assert client.insert_calls == []
    assert not hasattr(client, "list_events")  # never search-by-title (ADR D2)
    assert len(client.patch_calls) == 1
    _, patched_event_id, _ = client.patch_calls[0]
    assert patched_event_id == stored_event_id

    updated_state = db_manager.fetch_calendar_event_states()[0]
    assert updated_state["gcal_event_id"] == stored_event_id


# ---------------------------------------------------------------------------
# Case 12 — date became NULL: event/state untouched, anomaly flagged
# ---------------------------------------------------------------------------


def test_null_date_flags_anomaly_without_touching_event(
    db_manager, mock_settings, sample_opportunity
):
    opp = sample_opportunity(deadline="17-Aug-2026 05:30 PM")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-12")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    # Direct SQL, per spec case 12: the COALESCE guard (B1) blocks the normal
    # email follow-up path from ever nulling a stored date, so simulate the
    # rare case another way.
    db_manager.connection.execute(
        "UPDATE opportunities SET deadline = NULL WHERE id = ?;", (opp_id,)
    )
    db_manager.connection.commit()

    before = db_manager.fetch_calendar_event_states()[0]

    client.insert_calls.clear()
    client.patch_calls.clear()

    result = engine.sync()

    assert result.inserted == 0
    assert result.patched == 0
    assert any("date became empty" in line for line in result.flagged)
    assert client.insert_calls == []
    assert client.patch_calls == []

    after = db_manager.fetch_calendar_event_states()[0]
    assert after["status"] == "active"
    assert after["gcal_event_id"] == before["gcal_event_id"]
    assert after["content_hash"] == before["content_hash"]


# ---------------------------------------------------------------------------
# Case 13 — past event marked done, then frozen against further changes
# ---------------------------------------------------------------------------


def test_past_event_marked_done_and_frozen(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="15 June 2020")
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-13")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)

    result1 = engine.sync()
    assert result1.inserted == 1
    assert result1.marked_done == 1
    state = db_manager.fetch_calendar_event_states()[0]
    assert state["status"] == "done"

    # A title-changing update arrives, but the row is frozen -- no patch.
    changed = sample_opportunity(company_name="Microsoft Renamed", deadline="15 June 2020")
    db_manager.insert_or_update_opportunity(changed, source_thread_id="thread-13")

    client.patch_calls.clear()
    result2 = engine.sync()

    assert result2.patched == 0
    assert client.patch_calls == []
    state_after = db_manager.fetch_calendar_event_states()[0]
    assert state_after["status"] == "done"


# ---------------------------------------------------------------------------
# Case 14 — terminal drive: grace period, then retitle + cancelled, never delete
# ---------------------------------------------------------------------------


def test_terminal_drive_grace_period_then_retitle_cancelled(
    db_manager, mock_settings, sample_opportunity
):
    opp = sample_opportunity(deadline="17 June 2030")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-14")
    # A decoy keeps fetch_active_drives_only() non-empty once `opp` turns
    # terminal, so the partial-fetch guard doesn't also suppress this pass.
    decoy = sample_opportunity(company_name="Decoy Co", deadline="17 June 2030")
    db_manager.insert_or_update_opportunity(decoy, source_thread_id="thread-14-decoy")

    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    db_manager.connection.execute(
        "UPDATE opportunities SET current_status = 'REJECTED' WHERE id = ?;", (opp_id,)
    )
    db_manager.connection.commit()

    # Still inside the grace period -- untouched.
    result_inside_grace = engine.sync()
    assert result_inside_grace.retitled_stale == 0

    # Age last_seen_active_at past the grace period.
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db_manager.connection.execute(
        "UPDATE calendar_events SET last_seen_active_at = ? WHERE opportunity_id = ?;",
        (old_ts, opp_id),
    )
    db_manager.connection.commit()

    client.patch_calls.clear()
    result = engine.sync()

    assert result.retitled_stale == 1
    matching_patches = [c for c in client.patch_calls if c[2].get("summary", "").startswith("[?] ")]
    assert len(matching_patches) == 1

    state = next(
        s for s in db_manager.fetch_calendar_event_states() if s["opportunity_id"] == opp_id
    )
    assert state["status"] == "cancelled"  # REJECTED is a terminal current_status

    # The client has no delete method at all -- assert it's never been given one.
    assert not hasattr(client, "delete_event")
    assert not hasattr(client, "delete")


def test_reactivated_stale_drive_restores_google_title(
    db_manager, mock_settings, sample_opportunity
):
    """A drive retitled '[?] ...' by the stale pass must have its real title
    restored on Google once it reappears as active -- even when its dates are
    byte-for-byte unchanged, because ``set_calendar_event_status`` only ever
    touches ``status``, so the stored ``content_hash`` still matches the
    pre-'[?]' title and a naive hash-equality check would wrongly call this
    "unchanged" and leave the stale-looking title on Google forever."""
    opp = sample_opportunity(deadline="17 June 2030")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-14b")
    decoy = sample_opportunity(company_name="Decoy Co", deadline="17 June 2030")
    db_manager.insert_or_update_opportunity(decoy, source_thread_id="thread-14b-decoy")

    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    db_manager.connection.execute(
        "UPDATE opportunities SET current_status = 'REJECTED' WHERE id = ?;", (opp_id,)
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db_manager.connection.execute(
        "UPDATE calendar_events SET last_seen_active_at = ? WHERE opportunity_id = ?;",
        (old_ts, opp_id),
    )
    db_manager.connection.commit()

    engine.sync()  # retitles it to "[?] ..." and marks it 'stale'/'cancelled'
    state = next(
        s for s in db_manager.fetch_calendar_event_states() if s["opportunity_id"] == opp_id
    )
    assert state["title"].startswith("[?] ") is False  # stored title column is untouched
    assert state["status"] in ("stale", "cancelled")
    stored_event_id = state["gcal_event_id"]

    # The drive reactivates with the exact same deadline as before.
    db_manager.connection.execute(
        "UPDATE opportunities SET current_status = 'OPEN' WHERE id = ?;", (opp_id,)
    )
    db_manager.connection.commit()

    client.patch_calls.clear()
    result = engine.sync()

    assert result.patched == 1
    _, patched_event_id, patched_body = client.patch_calls[0]
    assert patched_event_id == stored_event_id
    assert not patched_body["summary"].startswith("[?] ")

    reactivated_state = next(
        s for s in db_manager.fetch_calendar_event_states() if s["opportunity_id"] == opp_id
    )
    assert reactivated_state["status"] == "active"


# ---------------------------------------------------------------------------
# Case 15 — partial-fetch guard: empty active-drive fetch aborts the stale pass
# ---------------------------------------------------------------------------


def test_partial_fetch_guard_skips_stale_pass(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17 June 2030")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-15")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    # Make fetch_active_drives_only() return [] and age the state well past
    # the grace period -- a naive implementation might retitle it anyway.
    db_manager.connection.execute(
        "UPDATE opportunities SET current_status = 'REJECTED' WHERE id = ?;", (opp_id,)
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    db_manager.connection.execute(
        "UPDATE calendar_events SET last_seen_active_at = ? WHERE opportunity_id = ?;",
        (old_ts, opp_id),
    )
    db_manager.connection.commit()

    assert db_manager.fetch_active_drives_only() == []

    client.patch_calls.clear()
    result = engine.sync()

    assert result.retitled_stale == 0
    assert client.patch_calls == []
    state = db_manager.fetch_calendar_event_states()[0]
    assert state["status"] == "active"


# ---------------------------------------------------------------------------
# Case 16 — dry-run: plan counted, zero service calls, zero DB writes
# ---------------------------------------------------------------------------


def test_dry_run_counts_but_writes_and_calls_nothing(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17-Aug-2026 05:30 PM")
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-16")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)

    result = engine.sync(dry_run=True)

    assert result.dry_run is True
    assert result.inserted == 1
    assert client.ensure_calendar_calls == 0
    assert client.insert_calls == []
    assert client.patch_calls == []
    assert client.get_calls == []
    assert db_manager.fetch_calendar_event_states() == []


# ---------------------------------------------------------------------------
# Case 17 — rebuild: missing event re-inserted, drifted event patched
# ---------------------------------------------------------------------------


def test_rebuild_reinserts_missing_and_patches_drifted(
    db_manager, mock_settings, sample_opportunity
):
    opp = sample_opportunity(deadline="17-Aug-2026 05:30 PM")
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-17a")
    other = sample_opportunity(company_name="Other Co", deadline="20-Aug-2026 05:30 PM")
    db_manager.insert_or_update_opportunity(other, source_thread_id="thread-17b")

    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    states = db_manager.fetch_calendar_event_states()
    assert len(states) == 2
    missing_state, drifted_state = states[0], states[1]

    # Simulate the first event having been deleted directly on Google.
    del client._events[missing_state["gcal_event_id"]]
    # Simulate the second event having drifted (edited on Google directly).
    client._events[drifted_state["gcal_event_id"]]["summary"] = "Edited directly on Google"

    client.insert_calls.clear()
    client.patch_calls.clear()
    client.get_calls.clear()

    result = engine.rebuild()

    assert len(client.get_calls) == 2
    assert result.inserted == 1
    assert result.patched == 1
    assert len(client.insert_calls) == 1
    assert len(client.patch_calls) == 1

    new_states = {s["id"]: s for s in db_manager.fetch_calendar_event_states()}
    assert new_states[missing_state["id"]]["gcal_event_id"] != missing_state["gcal_event_id"]


# ---------------------------------------------------------------------------
# Case 18 — auth-dead propagates uncaught
# ---------------------------------------------------------------------------


def test_auth_dead_propagates_uncaught(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17-Aug-2026 05:30 PM")
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-18")

    class DeadAuthClient(FakeCalendarClient):
        def ensure_calendar(self, name: str) -> str:
            raise CalendarAuthenticationError("OAuth dead — re-consent needed for Calendar")

    engine = CalendarSyncEngine(db_manager, DeadAuthClient(), mock_settings)

    with pytest.raises(CalendarAuthenticationError):
        engine.sync()
