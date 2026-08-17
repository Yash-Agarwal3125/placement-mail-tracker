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
    """Minimal stand-in for GoogleCalendarClient."""

    def __init__(self) -> None:
        self.calendar_id = "cal-vit-placements"
        self.ensure_calendar_calls = 0
        self.insert_calls: list[tuple[str, dict[str, Any]]] = []
        self.patch_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
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

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.delete_calls.append((calendar_id, event_id))
        self._events.pop(event_id, None)


# ---------------------------------------------------------------------------
# Case 9 — new drive inserts once, extendedProperties carry drive_id + opportunity_id
# ---------------------------------------------------------------------------


def test_new_drive_inserts_once(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17-Aug-2027 05:30 PM")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
    opp = sample_opportunity(deadline="17-Aug-2027 05:30 PM")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
    opp = sample_opportunity(oa_date="10 August 2027")
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-11")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, settings)

    result1 = engine.sync()
    assert result1.inserted == 1
    stored_event_id = db_manager.fetch_calendar_event_states()[0]["gcal_event_id"]

    # Follow-up email on the same thread reschedules the OA date.
    followup = sample_opportunity(oa_date="17 August 2027")
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
    opp = sample_opportunity(deadline="17-Aug-2027 05:30 PM")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
# docs/design/16 Phase 6: a still-active drive reclassified as no longer
# relevant (drive_kind or eligibility) gets retitled '[?] ...' and frozen,
# the same non-destructive treatment as a vanished drive -- distinct from
# the generic null-date case above, which stays untouched.
# ---------------------------------------------------------------------------


def test_not_matched_roster_verdict_retitles_and_freezes(
    db_manager, mock_settings, sample_opportunity
):
    """docs/design/16 Phase D/E + Cause 2 / Phase 3: a NOT_MATCHED roster
    verdict for a round retires that round's event, and the exclusion
    cascades forward to later rounds of the same drive too (selection is a
    ladder -- see ``calendar_sync.derive.is_round_excluded``)."""
    opp = sample_opportunity(
        oa_date="17-Aug-2027 05:30 PM", interview_date="20-Aug-2027 10:00 AM"
    )
    opp_id, _ = db_manager.insert_or_update_opportunity(
        opp, source_thread_id="thread-notmatched", email_classification="OA_UPDATE"
    )
    db_manager.set_my_status(
        db_manager.fetch_opportunity_by_id(opp_id)["drive_id"], "APPLIED", source="sheet"
    )
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    states_before = {s["event_type"]: s for s in db_manager.fetch_calendar_event_states()}
    assert set(states_before) == {"OA", "INTERVIEW"}

    db_manager.upsert_roster_verdict(opp_id, "OA", "NOT_MATCHED", method="registration_no")

    result = engine.sync()

    assert result.deleted == 2  # OA directly, INTERVIEW via cascade
    assert len(client.delete_calls) == 2
    states_after = {s["event_type"]: s for s in db_manager.fetch_calendar_event_states()}
    assert states_after["OA"]["status"] == "excluded"
    assert states_after["OA"]["gcal_event_id"] is None
    assert states_after["INTERVIEW"]["status"] == "excluded"
    assert states_after["INTERVIEW"]["gcal_event_id"] is None

    # Flicker guard: a second sync pass with the same verdicts must be a
    # pure no-op -- derive_events() and _null_date_pass share the same
    # resolver, so nothing gets re-created only to be deleted again.
    client.insert_calls.clear()
    client.delete_calls.clear()
    result2 = engine.sync()
    assert result2.inserted == 0
    assert result2.deleted == 0
    assert client.insert_calls == []
    assert client.delete_calls == []


def test_cascaded_exclusion_deletes_not_merely_flags_when_event_predates_verdict(
    db_manager, mock_settings, sample_opportunity
):
    """The other ordering than the test above: the INTERVIEW event state
    row already exists (from a run before the OA verdict landed), and only
    the *earlier* round (OA) ever gets a direct NOT_MATCHED verdict --
    INTERVIEW itself never does. If derive_events() (which drops the key
    from ``desired``) and ``_null_date_pass`` (which decides whether the
    dropped key is deleted or merely flagged) disagreed on the cascade, this
    is exactly the shape that would produce a permanent ``flagged`` anomaly
    line instead of a clean deletion -- the flicker the plan warns about."""
    opp = sample_opportunity(
        oa_date="17-Aug-2027 05:30 PM", interview_date="20-Aug-2027 10:00 AM"
    )
    opp_id, _ = db_manager.insert_or_update_opportunity(
        opp, source_thread_id="thread-cascade-order", email_classification="OA_UPDATE"
    )
    db_manager.set_my_status(
        db_manager.fetch_opportunity_by_id(opp_id)["drive_id"], "APPLIED", source="sheet"
    )
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()  # both OA and INTERVIEW state rows now exist, status='active'

    # Only the earlier round gets a direct verdict -- INTERVIEW's exclusion
    # is purely inherited via the cascade, never asserted directly.
    db_manager.upsert_roster_verdict(opp_id, "OA", "NOT_MATCHED", method="registration_no")

    result = engine.sync()

    assert result.deleted == 2
    assert result.flagged == []  # not merely flagged -- actually deleted
    states_after = {s["event_type"]: s for s in db_manager.fetch_calendar_event_states()}
    assert states_after["INTERVIEW"]["status"] == "excluded"
    assert states_after["INTERVIEW"]["gcal_event_id"] is None


def test_ambiguous_roster_verdict_leaves_event_untouched(
    db_manager, mock_settings, sample_opportunity
):
    opp = sample_opportunity(oa_date="17-Aug-2027 05:30 PM")
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-ambiguous")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    db_manager.upsert_roster_verdict(opp_id, "OA", "AMBIGUOUS", method="none")

    result = engine.sync()

    assert result.retitled_stale == 0
    state = db_manager.fetch_calendar_event_states()[0]
    assert state["status"] == "active"


def test_reclassified_non_placement_drive_deletes_event(
    db_manager, mock_settings, sample_opportunity
):
    """docs/design/16, by explicit user choice: a confidently-excluded
    round's Google event is actually deleted, not just retitled."""
    opp = sample_opportunity(deadline="17-Aug-2027 05:30 PM")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-hackathon")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()
    stored = db_manager.fetch_calendar_event_states()[0]
    assert stored["gcal_event_id"] is not None

    # Simulate a backfill reclassifying a pre-existing row as non-placement.
    db_manager.connection.execute(
        "UPDATE opportunities SET drive_kind = 'HACKATHON' WHERE id = ?;", (opp_id,)
    )
    db_manager.connection.commit()

    result = engine.sync()

    assert result.deleted == 1
    assert not result.flagged  # excluded, not merely flagged
    assert len(client.delete_calls) == 1
    assert client.delete_calls[0][1] == stored["gcal_event_id"]

    state = next(
        s for s in db_manager.fetch_calendar_event_states() if s["opportunity_id"] == opp_id
    )
    assert state["status"] == "excluded"
    assert state["gcal_event_id"] is None


def test_not_eligible_drive_deletes_event(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="17-Aug-2027 05:30 PM")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-noteligible")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()

    db_manager.connection.execute(
        "UPDATE opportunities SET eligibility_status = 'NOT_ELIGIBLE_BRANCH' WHERE id = ?;",
        (opp_id,),
    )
    db_manager.connection.commit()

    result = engine.sync()

    assert result.deleted == 1
    state = next(
        s for s in db_manager.fetch_calendar_event_states() if s["opportunity_id"] == opp_id
    )
    assert state["status"] == "excluded"
    assert state["gcal_event_id"] is None


def test_reclassified_drive_reverts_to_placement_reinserts_fresh_event(
    db_manager, mock_settings, sample_opportunity
):
    """A false-positive reclassification is reversible: fixing drive_kind
    back to PLACEMENT re-adds the event to Google. Deletion (unlike the old
    retitle-freeze) leaves nothing to PATCH, so this must go through
    ``_handle_insert``, not ``_handle_patch``."""
    opp = sample_opportunity(deadline="17-Aug-2027 05:30 PM")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-falsepos")
    client = FakeCalendarClient()
    engine = CalendarSyncEngine(db_manager, client, mock_settings)
    engine.sync()
    original_event_id = db_manager.fetch_calendar_event_states()[0]["gcal_event_id"]

    db_manager.connection.execute(
        "UPDATE opportunities SET drive_kind = 'HACKATHON' WHERE id = ?;", (opp_id,)
    )
    db_manager.connection.commit()
    engine.sync()
    assert db_manager.fetch_calendar_event_states()[0]["gcal_event_id"] is None

    db_manager.connection.execute(
        "UPDATE opportunities SET drive_kind = 'PLACEMENT' WHERE id = ?;", (opp_id,)
    )
    db_manager.connection.commit()

    result = engine.sync()

    assert result.inserted == 1
    assert result.patched == 0
    state = next(
        s for s in db_manager.fetch_calendar_event_states() if s["opportunity_id"] == opp_id
    )
    assert state["status"] == "active"
    assert state["gcal_event_id"] is not None
    assert state["gcal_event_id"] != original_event_id  # a genuinely new Google event


# ---------------------------------------------------------------------------
# Case 13 — past event marked done, then frozen against further changes
# ---------------------------------------------------------------------------


def test_past_event_marked_done_and_frozen(db_manager, mock_settings, sample_opportunity):
    opp = sample_opportunity(deadline="15 June 2020")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
    changed["priority"] = "HIGH"
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
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
    opp_id, _ = db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-14")
    # A decoy keeps fetch_active_drives_only() non-empty once `opp` turns
    # terminal, so the partial-fetch guard doesn't also suppress this pass.
    decoy = sample_opportunity(company_name="Decoy Co", deadline="17 June 2030")
    decoy["priority"] = "HIGH"
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

    # The stale pass (a vanished/terminal drive) still only retitles -- real
    # deletion is scoped to the null-date pass's confidently-excluded
    # branch only (docs/design/16), by explicit user choice.
    assert client.delete_calls == []


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
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
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
    opp = sample_opportunity(deadline="17-Aug-2027 05:30 PM")
    opp["priority"] = "HIGH"  # Phase 7: DEADLINE events need demonstrated interest/HIGH priority
    db_manager.insert_or_update_opportunity(opp, source_thread_id="thread-17a")
    other = sample_opportunity(company_name="Other Co", deadline="20-Aug-2027 05:30 PM")
    other["priority"] = "HIGH"
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
