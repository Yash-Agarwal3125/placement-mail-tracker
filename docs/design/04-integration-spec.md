# 04 — Integration Spec: Calendar Sync

Implementation contract only — **no bodies**. All column/function names below
are the real ones verified in `01-findings.md`; decisions trace to
`03-adr-calendar-sync.md` (D1…D8) and issues to `02-issues.md`.

---

## 1. Schema: `calendar_events`

Mechanism (per finding f): appended to the `executescript` in
`DatabaseManager.create_tables` (`pmt/db/manager.py:151-256`), idempotent
`CREATE TABLE IF NOT EXISTS` + indexes in the second script block
(`manager.py:258-279`). No `db/migrate.py` change needed (new table, no
backfill).

```sql
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    drive_id TEXT,                          -- display/trace only (ADR D1)
    event_type TEXT NOT NULL,               -- 'DEADLINE' | 'OA' | 'INTERVIEW'
    gcal_calendar_id TEXT,
    gcal_event_id TEXT,                     -- NULL until Google insert succeeds
    start_iso TEXT NOT NULL,                -- ISO8601 with offset, or YYYY-MM-DD when all_day=1
    end_iso TEXT NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    location TEXT,
    content_hash TEXT NOT NULL,             -- sha256(start_iso|end_iso|title|location)
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'done' | 'stale' | 'cancelled'
    last_seen_active_at TEXT,               -- UTC ISO; drives the stale grace period
    created_at TEXT NOT NULL,               -- utc_now_iso()
    updated_at TEXT NOT NULL,
    UNIQUE(opportunity_id, event_type),
    FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_status
    ON calendar_events(status);
CREATE INDEX IF NOT EXISTS idx_calendar_events_opportunity_id
    ON calendar_events(opportunity_id);
```

Input columns consumed from `opportunities` (real names): `id`, `drive_id`,
`company_name`, `role`, `deadline`, `oa_date`, `interview_date`,
`work_location`, `package_or_stipend`, `action_required`, `current_status`,
`status`, `eligibility_status`, `my_status`, `source_thread_id`,
`source_email_id`. (`next_event_date` is deliberately **not** evented — it is
derived from `oa_date`/`interview_date` at `runner.py:145-171` and would
duplicate them.)

## 2. Settings (`pmt/config/settings.py`, existing Field/alias pattern)

| Field | Alias (env) | Default | Notes |
|---|---|---|---|
| `calendar_sync_enabled: bool` | `CALENDAR_SYNC_ENABLED` | `False` | rollout gate |
| `calendar_sync_mode: str` | `CALENDAR_SYNC_MODE` | `"applied_only"` | governs OA/INTERVIEW only; DEADLINE always all-eligible (ADR D6). Values: `applied_only` \| `all_eligible` |
| `calendar_name: str` | `CALENDAR_NAME` | `"VIT Placements"` | find-or-create by summary |
| `calendar_token_file: str` | `CALENDAR_TOKEN_FILE` | `"config/calendar_token.json"` | third token stack (ADR D4) |
| `calendar_timezone: str` | `CALENDAR_TIMEZONE` | `"Asia/Kolkata"` | applied via stdlib `zoneinfo` (ADR D3) |
| `calendar_deadline_reminder_minutes: list[int]` | `CALENDAR_DEADLINE_REMINDER_MINUTES` | `[1440]` | 24 h |
| `calendar_event_reminder_minutes: list[int]` | `CALENDAR_EVENT_REMINDER_MINUTES` | `[1440, 60]` | 24 h + 1 h for OA/INTERVIEW |
| `calendar_stale_after_hours: float` | `CALENDAR_STALE_AFTER_HOURS` | `48.0` | grace period before `[?]` retitle |

Credentials file: reuse `config/credentials.json` (same client secret as
Gmail/Sheets — `settings.py:24-27, 48-51`); no new setting.

`ConfigValidator` addition: when `calendar_sync_enabled`, a check with
`component="calendar"` (pattern at `pmt/config/validator.py:162-208`)
verifying credentials file presence and warning (not error) when
`calendar_token.json` is absent ("first run will require interactive
consent"). Note `main._apply_validation_results` tracks components
`{"database","gmail","sheets","notifications"}` (`main.py:99`) — add
`"calendar"` there so a calendar validation warning routes to
`mark_component` instead of a bare warning.

## 3. Module interfaces — `pmt/calendar_sync/`

Package name `calendar_sync` (never `calendar` — stdlib shadow). Signatures
and docstring-level behaviour only.

### 3.1 `client.py` — Calendar API wrapper

Mirrors the Sheets client seams: injectable `service`, typed auth error,
`last_error` attribute (`sheets_sync.py:122-146`).

```
class CalendarAuthenticationError(RuntimeError):
    """Raised when Calendar OAuth cannot be completed or the refresh token is
    dead ('OAuth dead — re-consent needed for Calendar')."""

class GoogleCalendarClient:
    def __init__(self, settings: Settings, *, service: Resource | None = None) -> None:
        """Store settings; lazily build the 'calendar' v3 service on first use.
        `service` injection is the test seam (same as GoogleSheetsSync)."""

    def authenticate(self) -> Credentials:
        """Load config/calendar_token.json → refresh if expired → else run the
        interactive flow. Copies sheets_sync.authenticate (:581-606) with two
        deltas (ADR D4): refresh() is wrapped — RefreshError/invalid_grant →
        CalendarAuthenticationError with the re-consent message; and the
        interactive run_local_server flow is only attempted when stdin is a
        TTY, else raises CalendarAuthenticationError immediately."""

    def ensure_calendar(self, name: str) -> str:
        """Return the calendarId whose summary == name, creating it (with
        settings.calendar_timezone) if absent. One calendarList.list per run."""

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> str:
        """events().insert; returns the new Google event id. body includes
        extendedProperties.private = {drive_id, opportunity_id} (ADR D1)."""

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        """events().patch by stored id — never search-by-title."""

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        """events().get; returns None on 404 (used only by rebuild)."""

    last_error: str | None   # same contract as GoogleSheetsSync.last_error
```

Retry: the same transient-retry envelope as sheets
(`sheets_sync.py:152-187`: 3 attempts on HttpError 429/5xx and socket
errors, backoff 2 s/5 s) applied around the sync engine's API calls — factored
as a small helper inside `client.py`, not copied per call site.

### 3.2 `derive.py` — drive rows → desired events (pure; no I/O, no API)

```
class CalendarEvent(BaseModel):        # pydantic, like ai/models.PlacementExtraction
    opportunity_id: int
    drive_id: str | None
    event_type: Literal["DEADLINE", "OA", "INTERVIEW"]
    title: str                          # "Company — OA" / "Company — Apply by deadline" etc.
    start_iso: str                      # tz-aware ISO (+05:30) or YYYY-MM-DD when all_day
    end_iso: str
    all_day: bool
    location: str | None                # opportunities.work_location
    description: str                    # role, package_or_stipend, action_required,
                                        # drive_id, Gmail link (source_thread_id pattern
                                        # from sheets_sync._gmail_link :897-905)
    reminder_minutes: list[int]

    def content_hash(self) -> str:
        """sha256(start_iso|end_iso|title|location) — the diff key (ADR D2)."""


def derive_events(
    opportunities: list[dict[str, Any]],   # rows from fetch_active_drives_only()
    settings: Settings,
) -> tuple[list[CalendarEvent], list[str]]:
    """Map each visible drive to 0–3 events; returns (events, anomalies).

    Filtering: skip rows whose eligibility_status contains 'NOT_ELIGIBLE'
    (sheet rule, sheets_sync.py:205-208) and unidentifiable companies
    (frozenset rule, runner.py:77 / alert_generator.py:15). DEADLINE from
    `deadline` for every visible drive; OA from `oa_date` / INTERVIEW from
    `interview_date` only when calendar_sync_mode == 'all_eligible' OR
    my_status not in ('NOT_APPLIED', '', None) (ADR D6).

    Dates: parse raw strings with utils.time.parse_event_datetime (strict);
    fuzzy-only or unparseable strings yield no event + an anomaly line (B4).
    No time-of-day token in the raw string → all_day=True (B7): DEADLINE
    all-day on the deadline date (diff treats end as 23:59 local); OA/INTERVIEW
    all-day on the event date. Timed: OA/INTERVIEW 1 h duration; DEADLINE a
    30 min block ending at the deadline instant. Localize with
    zoneinfo.ZoneInfo(settings.calendar_timezone) (ADR D3).

    Collision guard (B6): two events from *different* opportunity_ids with the
    same (normalized company_name, event_type, date) → keep the first
    (lower opportunity_id), drop the second, append an anomaly line.

    Pure function: consumes dict rows, parses nothing from email, calls no API."""
```

```
# addition to pmt/utils/time.py (new function; parse_datetime_flexible untouched)
def parse_event_datetime(date_str: str) -> datetime | None:
    """Strict variant for the calendar boundary (ADR D3): dateutil with
    fuzzy=False (or the existing strptime list when dateutil is absent),
    same year-range/bare-year guards as parse_datetime_flexible. Returns
    naive local; caller localizes."""
```

### 3.3 `sync.py` — diff engine

```
@dataclass(slots=True)
class CalendarSyncResult:
    inserted: int = 0
    patched: int = 0
    unchanged: int = 0
    marked_done: int = 0
    retitled_stale: int = 0
    flagged: list[str] = field(default_factory=list)   # digest anomaly lines
    dry_run: bool = False


class CalendarSyncEngine:
    def __init__(
        self,
        database: DatabaseManager,
        client: GoogleCalendarClient,
        settings: Settings,
    ) -> None: ...

    def sync(self, *, dry_run: bool = False) -> CalendarSyncResult:
        """One diff pass (ADR D2, table of rules):

        1. rows = database.fetch_active_drives_only(); if empty → abort the
           stale pass (partial-fetch guard) but still return (no retitles).
        2. desired, anomalies = derive_events(rows, settings).
        3. calendar_id = client.ensure_calendar(settings.calendar_name).
        4. Per desired event, look up calendar_events by
           (opportunity_id, event_type):
             - no row → insert_event, then upsert state row with gcal_event_id
               (state written only after the API call succeeds);
             - row.status == 'done' → skip (frozen);
             - hash equal → unchanged;
             - hash differs → patch_event by row.gcal_event_id, update row.
           Every matched row gets last_seen_active_at = utc_now_iso().
        5. Done pass: any 'active' row whose end < now (all_day: end-of-day
           local) → status='done', no API call.
        6. Null-date pass: drive still in rows but a previously-evented date
           column is now NULL → leave the Google event and the state row
           untouched, append anomaly (post-B1 this is rare and meaningful).
        7. Stale pass (skipped when step 1 aborted): 'active' rows whose
           opportunity_id is absent from rows AND last_seen_active_at older
           than calendar_stale_after_hours → one patch retitling to
           '[?] <title>', status = 'cancelled' if the drive row exists with a
           terminal current_status (REJECTED/WITHDRAWN/EXPIRED/COMPLETED,
           vocabulary at manager.py:42-55), else 'stale'. NEVER any delete —
           the engine has no delete method at all.
        8. dry_run=True: identical traversal; every would-be API/DB write is
           logged at INFO ('PLAN: insert DEADLINE Microsoft …') and counted;
           nothing is written.

        Anomalies land in CalendarSyncResult.flagged; the runner stores them
        for the next digest (§4). Raises nothing for per-event API errors —
        logs, counts, continues; raises CalendarAuthenticationError (auth
        dead) and lets transient-retry exhaustion propagate to the runner's
        component handler."""

    def rebuild(self) -> CalendarSyncResult:
        """Reconciliation (ADR D7): for every calendar_events row —
        get_event(gcal_event_id); None → clear stored id, re-insert, store new
        id; present with hash mismatch → patch. Never deletes rows or events."""

    last_error: str | None
```

`DatabaseManager` additions (query helpers, same style as `sent_alerts`
helpers at `manager.py:469-499`):

```
def upsert_calendar_event_state(self, event: "CalendarEvent", *,
    gcal_calendar_id: str, gcal_event_id: str | None, status: str = "active") -> int:
    """INSERT … ON CONFLICT(opportunity_id, event_type) DO UPDATE (pattern:
    log_processed_email, manager.py:713-753)."""

def fetch_calendar_event_states(self, *, status: str | None = None) -> list[dict[str, Any]]: ...

def set_calendar_event_status(self, row_id: int, status: str) -> None: ...

def bulk_update_my_status(self, my_status_by_drive_id: dict[str, str]) -> int:
    """A1 write-back: UPDATE opportunities SET my_status=? WHERE drive_id=? AND
    my_status != ? (skip no-ops). Called from the sheets sync (§4). Sheet
    labels are reverse-mapped to enum values via _MY_STATUS_LABELS
    (sheets_sync.py:847-855). Returns changed-row count."""
```

## 4. Orchestrator & report diff (described, not coded)

1. **`pmt/reliability/status.py`** — `RunReport` gains `calendar_ok: bool =
   True` (after `notifications_ok`, `status.py:44`); the `status` property's
   `all([...])` list (`status.py:90-98`) gains it. *(A5 / ADR D5)*
2. **`main.py`** — `_merge_report` (`main.py:118-127`) merges `calendar_ok`;
   `_apply_validation_results` tracked-components set (`main.py:99`) gains
   `"calendar"`; `argparse` added in `main()` with `--calendar-dry-run` and
   `--calendar-rebuild` (both `store_true`), threaded to the runner. *(A8)*
   `_finalize_run`: heartbeat written on PARTIAL_SUCCESS too, payload gains
   the status string. *(B5)*
3. **`pmt/scheduler/runner.py`** — in `_execute_sync_pipelines`, between the
   sheets-failure block (ends `runner.py:649`) and the AlertGenerator block
   (starts `runner.py:651`): if `settings.calendar_sync_enabled` (or a
   calendar CLI flag is set), build `GoogleCalendarClient` +
   `CalendarSyncEngine` and call `sync(dry_run=…)` / `rebuild()` in its own
   try/except; on exception → `report.mark_component("calendar", False,
   str(e) or engine.last_error, critical=False)`. `result.flagged` lines are
   persisted for the digest (simplest storage matching existing patterns: a
   `calendar_flags` key in a small JSON under `data/`, or rows in the existing
   `notifications` table with `channel='digest_pending'` — implementer's
   choice, documented in code). *(ADR D5/D7; A7)*
4. **`pmt/sheets/sheets_sync.py`** — `_sync_active_opportunities_internal`,
   right after `my_status_map = self._read_my_status_map()`
   (`sheets_sync.py:211`): call `database.bulk_update_my_status(my_status_map)`.
   `_read_my_status_map` (`:430-458`): API exception no longer returns `{}` —
   it raises into the existing retry wrapper; the true-empty-sheet case still
   returns `{}`; and row building falls back to DB `my_status` (it already
   does — `:652-655` — which becomes correct once write-back exists). *(A1/B2
   / ADR D6)*
5. **`pmt/db/manager.py`** — B1 fix: `deadline = COALESCE(:deadline,
   deadline)` and same for `interview_date`, `oa_date` in
   `_update_opportunity_row` (`manager.py:923-926`). *(B1 / ADR D8)*
6. **`pmt/scheduler/alert_generator.py`** — B3 fix: alert_type becomes
   date-suffixed (`f"EVENT_24H:{event_date:%Y-%m-%d}"`) in
   `_check_deadline_alerts`/`_check_event_alerts`; `sent_alerts` schema
   unchanged. *(B3 / ADR D8)*
7. **`pmt/gmail/gmail_client.py` + `pmt/sheets/sheets_sync.py`** — A3 fix:
   wrap `credentials.refresh(Request())` (`gmail_client.py:164`,
   `sheets_sync.py:589`) in try/except RefreshError → raise the stack's typed
   auth error with the "OAuth dead — re-consent needed for <service>"
   message; skip `run_local_server` on non-TTY. *(A3 / ADR D4)*
8. **`pmt/scheduler/digest_generator.py`** — new "Calendar flags" section in
   `_format_digest` fed from the persisted `flagged` lines (cleared once
   digested). *(diff rules: "flag in digest")*

Pipeline after the change (unchanged steps in parentheses):
(Digest → Fetch → Extract/Store) → Sheets [+ My Status write-back] →
**Calendar** → Alerts → (main: streak counter → heartbeat).

## 5. Test plan

Conventions per finding i: pytest + `unittest.mock`, in-memory
`db_connection`/`db_manager`/`mock_settings`/`sample_opportunity` fixtures
from `tests/conftest.py`, fake `service` injected into clients, pure-function
tests for derive (mirroring `test_sheet_sync.py`'s row-builder style).

New fixture: `fake_calendar_service` — `MagicMock` recording
`events().insert/patch/get` calls; `mock_settings` gains the calendar fields.

### `tests/test_calendar_derive.py` (pure)
1. Drive with timed `oa_date` ("17-Jun-2026 05:30 PM") → one OA event,
   `start_iso` ends `+05:30`, 1 h duration, reminders `[1440, 60]`.
2. Date-only `deadline` ("15 June 2026") → all-day DEADLINE event (`all_day`,
   `YYYY-MM-DD` start), reminders `[1440]`. *(B7)*
3. Fuzzy-only garbage ("Round 3 at 5 in Lab 2") → no event + anomaly. *(B4)*
4. Bare-year / out-of-range date → no event + anomaly (guards inherited from
   `time.py:97-99, 117-123`).
5. `applied_only` mode: drive `my_status='NOT_APPLIED'` → DEADLINE yes,
   OA/INTERVIEW no; `my_status='APPLIED'` → all three. `all_eligible` → all
   three regardless. *(ADR D6)*
6. `eligibility_status='NOT_ELIGIBLE_BRANCH'` or company "Unknown" → zero
   events.
7. Collision: two opportunity_ids, same company/event_type/date → one event +
   anomaly. *(B6)*
8. `content_hash` stable across runs; changes when title/start/location change.

### `tests/test_calendar_sync.py` (engine + fake service + real in-memory DB)
9. New drive → `insert` called once; state row has `gcal_event_id`,
   `status='active'`; `extendedProperties.private` carries drive_id +
   opportunity_id.
10. Second sync, nothing changed → **zero** insert/patch calls, all
    `unchanged`.
11. **Reschedule simulation** *(required case)*: seed drive with
    `oa_date='10 June 2026'`, sync; deliver a follow-up via
    `insert_or_update_opportunity` (same `source_thread_id`) with
    `oa_date='17 June 2026'`; sync → exactly one `patch` with the **stored**
    event id (assert no insert, no list/search call), state hash updated.
12. Date became NULL (direct SQL, since B1 blocks the email path) → event
    untouched, anomaly flagged, row stays `active`. 
13. Past event (`end < now`) → row `done`; subsequent title change → no patch
    (frozen).
14. Drive turns terminal (`current_status='REJECTED'`) with future event →
    within grace: untouched; after `calendar_stale_after_hours` (freeze time
    or manipulate `last_seen_active_at`) → one patch retitling `[?] …`,
    status `cancelled`; **assert the fake service's delete was never
    called** (the client has no delete, assert no unknown method use).
15. Partial-fetch guard: `fetch_active_drives_only` returns `[]` → no
    retitles, no patches, result notes aborted stale pass.
16. Dry-run: full plan counted, zero service calls, zero `calendar_events`
    rows.
17. Rebuild: state row whose `get_event` → None → re-insert + new id stored;
    hash-mismatched existing event → patch.
18. Auth dead: client raising `CalendarAuthenticationError` → runner marks
    `calendar` component, `RunReport.exit_code == 2`, `calendar_ok is False`.

### Upstream regression tests (ADR D8 prerequisites)
19. `test_database.py` additions *(B1/B8)*: follow-up changing `oa_date` →
    row updated + `updates` row written + `updated_at` bumped; follow-up
    omitting `oa_date`/`deadline` → stored values preserved.
20. `test_notifications.py` addition *(B3)*: after `EVENT_24H:<date1>` sent,
    reschedule to date2 → alert fires again with `EVENT_24H:<date2>`.
21. `test_sheet_sync.py`/`test_database.py` addition *(A1/B2)*:
    `bulk_update_my_status` writes sheet labels back as enum values, skips
    no-ops; read-back API failure raises (not `{}` + wipe).
22. `test_reliability_status.py` addition *(A5)*: `calendar_ok=False` →
    PARTIAL_SUCCESS/exit 2; `summary_lines` includes the Calendar line.
23. Heartbeat *(B5)*: PARTIAL_SUCCESS run updates heartbeat with status
    recorded; FAILED does not.

## 6. Rollout checklist

1. Land ADR-D8 upstream fixes as separate small changes, each with its tests,
   `python -m pytest` and `ruff check .` green (Python 3.10 target).
2. Add settings fields + `ConfigValidator` calendar check; `.env` untouched —
   defaults keep the feature off.
3. Land `calendar_sync/` module + `calendar_events` DDL + runner/report wiring
   with `CALENDAR_SYNC_ENABLED` unset (false): scheduled runs are
   behaviourally identical.
4. Verify the GCP OAuth consent screen is **"In production"** publishing
   status (Testing = 7-day refresh-token death); Calendar API enabled on the
   project.
5. Interactive machine session: `python main.py --calendar-dry-run` with
   `CALENDAR_SYNC_ENABLED=true` in the shell env only → mints
   `config/calendar_token.json` (consent), prints the plan, writes nothing.
   Review the plan against the ALL DRIVES tab.
6. Set `CALENDAR_SYNC_ENABLED=true` in `.env`; run `python main.py` once
   supervised: confirm "VIT Placements" calendar exists, events look right on
   the phone, exit code 0.
7. Let 2–3 scheduled cycles pass: confirm no-change runs log zero
   insert/patch calls; confirm heartbeat advances; check `logs/app.log` for
   `RUN_STATUS_JSON` including `calendar_ok`.
8. Reschedule drill: edit one test drive's `oa_date` in the DB (or wait for a
   real college reschedule) → next cycle PATCHes the event in place (same
   event id, phone shows moved event).
9. Failure drill: temporarily rename `calendar_token.json` → next run exits 2,
   sheet/alerts unaffected, streak alert after 3 failures names Calendar and
   the OAuth-dead message; restore token.
10. Confirm the next 8 AM digest renders the "Calendar flags" section (or its
    absence when clean).
