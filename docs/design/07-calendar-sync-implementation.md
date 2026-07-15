# 07 — Calendar Sync: Implementation Notes

Built per `docs/design/03-adr-calendar-sync.md` and `docs/design/04-integration-spec.md`.
This doc records what was actually built vs. spec, the gaps found and how they
were resolved (all approved during the session, none improvised silently),
the dry-run and live-run results against real production data, and a runbook
for extending the system later. It does not repeat the ADR's reasoning —
read `03-adr-calendar-sync.md` first for the "why".

---

## What was built

- `src/placement_mail_tracker/calendar_sync/` (new package):
  - `client.py` — `GoogleCalendarClient`: auth (mirrors `GoogleSheetsSync`'s
    pattern exactly, including refresh-wrapping and non-TTY fail-fast),
    `ensure_calendar`/`insert_event`/`patch_event`/`get_event`, a shared
    `_call_with_retry` helper (3 attempts, 2s/5s backoff, matches
    `sheets_sync.py`'s envelope). No delete method exists anywhere in this
    file — verified by a dedicated test that scans for `.delete(` calls.
  - `derive.py` — `CalendarEvent` (pydantic model + `content_hash()`) and
    `derive_events()`: opportunities rows → 0–3 events per drive, strict date
    parsing, `applied_only`/`all_eligible` mode gating, all-day detection,
    IST localization, a company/date collision guard.
  - `sync.py` — `CalendarSyncResult` + `CalendarSyncEngine`: the content-hash
    diff engine (`sync()`) and reconciliation (`rebuild()`), both dry-run
    capable.
- `calendar_events` table (idempotent DDL in `DatabaseManager.create_tables`)
  + 3 new `DatabaseManager` helpers (`upsert_calendar_event_state`,
  `fetch_calendar_event_states`, `set_calendar_event_status`).
- 7 new `Settings` fields (`calendar_sync_enabled`, `calendar_sync_mode`,
  `calendar_name`, `calendar_token_file`, `calendar_timezone`,
  `calendar_deadline_reminder_minutes`, `calendar_event_reminder_minutes`,
  `calendar_stale_after_hours`), a `ConfigValidator` calendar check, and
  `tzdata` added as a direct dependency (see "Known gaps" — Windows'
  `zoneinfo` needs it explicitly; it is not guaranteed present).
- Orchestrator wiring: `runner.py`'s `_execute_calendar_sync` (called from
  `_execute_sync_pipelines`, between Sheets and Alerts), `--calendar-dry-run`
  / `--calendar-rebuild` CLI flags in `main.py`, a "Calendar flags" digest
  section fed by a small persisted-JSON store
  (`scheduler/calendar_flags_store.py`).
- 44 new tests across `test_calendar_client.py` (12), `test_calendar_derive.py`
  (9), `test_calendar_sync.py` (12), `test_runner_calendar_integration.py` (4),
  `test_calendar_flags_store.py` (3), `test_validator.py` (+2),
  `test_notifications.py` (+2). Full suite: **440 passed** (396 before this
  session + 44 new), 14 deselected (opt-in eval), 0 regressions — verified
  against both system Python 3.10 and the actual project `.venv` (Python
  3.13.7, the interpreter `scripts/run_tracker.bat` activates for the real
  scheduled job).

## Spec gaps found and their resolutions (all approved before landing)

1. **`parse_event_datetime`**: the spec calls for a new strict-parse function
   in `utils/time.py`. `parse_datetime_strict` already existed (added in the
   prior extraction-reliability session) with a docstring explicitly citing
   this ADR as its reason for existing. Resolution: added a one-line named
   wrapper (`parse_event_datetime` → `parse_datetime_strict`) instead of a
   second parallel implementation. Flagged to the user before the derive
   subagent used it; no objection.
2. **Google Calendar's exclusive all-day `end.date`**: found during
   integration, not in the spec. `CalendarEvent`/`content_hash()` represent
   an all-day event's last *inclusive* day (`start_iso == end_iso`), but the
   Calendar API requires `end.date` to be the day *after* for a single-day
   all-day event, or it rejects the body. Fixed in `sync.py`'s `_build_body`/
   `_body_from_state` (add one day only in the wire body) and
   `_remote_matches_state` (subtract it back out before hashing, so
   `rebuild()`'s comparison isn't permanently mismatched). Covered by
   `test_all_day_event_body_has_exclusive_end_date`.
3. **`ensure_calendar` skipped entirely in dry-run** (sync-engine subagent's
   judgment call, confirmed correct): required to make "zero API calls" in
   dry-run true, and means `--calendar-dry-run` needs no `calendar_token.json`
   at all — useful, since it's the mode used for all pre-live verification.
4. **`rebuild()` excludes `status='done'` rows**: spec says "every
   `calendar_events` row"; judged this should mirror the normal diff's frozen
   rule for symmetry. `rebuild()` also cannot restore `description`/
   `reminder_minutes` (not columns on `calendar_events`) — a re-inserted event
   gets these back on the next normal `sync()` pass.
5. **Past-deadline drives on first sync**: derive_events doesn't skip a
   DEADLINE event just because the parsed date is already in the past (a
   real gap in this dataset — 7 of the first 8 real drives had this).
   User's explicit choice: **insert then immediately freeze `done`**, not
   skip derivation. Confirmed working exactly as designed against real data
   (see "Live-run results" below).

## Rollout status: LIVE for the scheduled job

Every live verification in Steps 5-6 (first run, reschedule drill, failure
drill) had worked only via a one-off `CALENDAR_SYNC_ENABLED=true` shell
environment variable, which does not persist — `.env` had no `CALENDAR_*`
keys, and `scripts/run_tracker.bat` (what Task Scheduler actually invokes)
reads only `.env`. This was caught before closing the session: with the
user's explicit go-ahead, `CALENDAR_SYNC_ENABLED=true` has now been added to
`.env` (confirmed picked up by `get_settings()` via the real `.venv` Python).
The next unattended 3-hourly run will include the calendar step for real —
this is ADR rollout checklist step 6, now complete.

## Known gaps / findings — not fixed this session, worth a look

- **`report.mark_component("google_sheets", ...)` in `runner.py`
  `_execute_sync_pipelines`**: `RunReport` only has a `sheets_ok` attribute,
  not `google_sheets_ok`. `mark_component`'s `hasattr` check silently no-ops,
  so a Sheets failure has likely never actually flipped `sheets_ok` to
  `False` in production. Found as a side effect of reading this file to wire
  the calendar step in next to it. **Not fixed** — out of this session's
  scope (extraction/runner reliability, not calendar), but it's a real bug in
  exit-code/alerting behavior and should be a fast follow-up (rename the
  component string to `"sheets"`).
- **The actual project `.venv` (Python 3.13.7 — confirmed via
  `scripts/run_tracker.bat` to be the interpreter the real scheduled job
  activates) was missing `tzdata`, `pypdf`, and `python-dateutil`** despite
  all three being in `requirements.txt` — they'd only ever been verified
  against a different system Python install during earlier testing. This is
  exactly the risk the `tzdata` addition flagged in Step 1: `zoneinfo` needs
  the IANA database explicitly on Windows, and it is not guaranteed present
  just because some other installed tool happens to pull it in transitively.
  Fixed by running `pip install -r requirements.txt` in the real `.venv`, and
  the full 440-test suite was then re-verified against that same `.venv`
  Python (not just system Python) to confirm the fix actually reaches the
  unattended job. There is still no CI/automated check that the venv stays
  in sync with `requirements.txt` going forward — worth adding if this
  recurs.
- **ADR rollout checklist step 5's claim** ("`--calendar-dry-run` ... mints
  `config/calendar_token.json`") **is now stale.** It predates the
  sync-engine subagent's judgment call (see gap #3 above) that dry-run skips
  auth entirely. The first real token mint happens on the first *non*-dry-run
  invocation (this session's Step 5), not dry-run. The ADR text itself is
  left as historical record per this session's scope lock; noting the drift
  here instead.
- **Mid-session OAuth client rotation**: partway through Step 5, the original
  GCP OAuth client hit `access_denied` (app in "Testing" publishing status,
  account not authorized for the Calendar scope). The user created a new GCP
  project + OAuth client and had all three services (Gmail, Sheets, Calendar)
  re-authenticated against it. `config/credentials.json` and all three token
  files were replaced; old versions kept alongside with a
  `.old_client502212604465` suffix (gitignored, harmless, deletable). No code
  changes were needed for this — Gmail/Sheets/Calendar already all resolved
  to the same `gmail_credentials_file` setting by design.

- **`mock_settings` (tests/conftest.py) reads the real `.env` for every field
  it doesn't explicitly override.** Adding `CALENDAR_SYNC_ENABLED=true` to
  `.env` this session immediately broke two new tests that assumed
  `mock_settings.calendar_sync_enabled` defaults to `False` — fixed by
  forcing the field explicitly via `model_copy()` rather than relying on the
  fixture's ambient value. Those two tests are now immune, but this was not
  a one-off: any other test elsewhere in the suite that implicitly relies on
  "whatever `.env` currently contains" for a field `mock_settings` doesn't
  override could break the same way the next time someone adds an unrelated
  `.env` key. A full audit of `mock_settings`-dependent tests for this
  coupling is a test-infrastructure concern, not a `calendar_sync` one — out
  of this session's scope, flagged here for a future pass.

## Confirmed NOT bugs

- `applied_only` mode correctly suppresses OA/INTERVIEW events until
  `my_status` leaves `NOT_APPLIED` — confirmed against real data (16 rows
  with real OA/interview dates correctly produced zero events under the
  default mode, since My Status write-back is new this session and nothing
  has been marked "Applied" yet).
- The `AttributeError` seen once in a Calendar auth-timeout traceback during
  Step 5 is internal to `google-auth-oauthlib`'s own WSGI timeout handling,
  not this codebase — the surrounding `WSGITimeoutError` was caught and
  reported correctly (`calendar_ok=False`, zero DB writes) regardless.

## Dry-run results (Step 4 checkpoint, real DB)

Run in isolation (no Gmail/Gemini/Sheets calls, no Calendar auth — dry-run
skips it) against the real 57-row active-drives table:

- 8 DEADLINE events derived, 0 OA/INTERVIEW (expected under `applied_only`
  with no drives yet marked "Applied")
- 13 rows filtered `NOT_ELIGIBLE`, 1 filtered as unidentified company
- 0 anomalies (no unparseable dates, no collisions)
- `CalendarSyncEngine.sync(dry_run=True)` ran end-to-end with zero errors,
  zero API calls, zero `calendar_events` rows written

## Live-run results (Steps 5–6, real Google Calendar)

- First real run: `inserted=8 patched=0 unchanged=0 marked_done=7
  retitled_stale=0`, `Calendar OK: True`, exit code 0 — 7 of 8 were
  already-past deadlines, inserted then immediately frozen per the user's
  choice above. Verified visually in the actual Google Calendar UI ("VIT
  Placements" calendar present, correct IST times).
- Reschedule drill: pushed one active drive's deadline out a week directly
  in the DB, re-ran → `patched=1`, same `gcal_event_id`, no duplicate,
  content_hash and DB row updated.
- Failure drill: simulated a dead Calendar token under a non-interactive
  (scheduled-run-like) condition → `sheets_ok=True` (unaffected),
  `calendar_ok=False`, `exit_code=2` (PARTIAL_SUCCESS, not FAILED), no
  uncaught exception. This also fired a real one-shot "Calendar OAuth dead"
  SMTP alert — confirms that path works end-to-end. Token restored and the
  alert dedup flag cleared afterward; a follow-up sync confirmed
  `unchanged=1`, zero API calls (steady state).

## Runbook: adding a 4th event type later

1. Add the new literal to `CalendarEvent.event_type`'s `Literal[...]` in
   `derive.py`.
2. Add a branch in `derive_events()` deriving it from whichever
   `opportunities` column holds the date (title/description/reminder rules
   follow the existing DEADLINE/OA/INTERVIEW pattern).
3. No `sync.py` or DB changes needed — the diff engine and
   `UNIQUE(opportunity_id, event_type)` key are already generic over
   `event_type`.
4. Add derive test cases mirroring cases 1–8; the sync engine's existing
   tests already cover the new type for free since they parametrize on
   `CalendarEvent`, not on the literal event_type values.

## Verification checklist (per the session's own scope contract)

- [x] Step 0 prerequisites verified against code, not assumed from docs
- [x] No insert/patch/delete call reached the live Google Calendar before
      Step 4 was explicitly approved
- [x] Identity key is `(opportunity_id, event_type)`, not `drive_id`
- [x] No delete method exists anywhere in `client.py` (test-enforced)
- [x] Timezone attachment point documented with file:line
      (`derive.py`'s `_derive_single_event`, `ZoneInfo(settings.calendar_timezone)`)
- [x] `--calendar-dry-run` was the only mode used for all verification prior
      to Step 5
- [x] All 23 spec test cases present, full suite green (440 passed), zero
      regressions
- [x] No extraction-pipeline files (`extraction/`, `ai/`) modified
- [x] Every spec gap was surfaced and approved before implementation —
      see "Spec gaps found" above; nothing was silently resolved
