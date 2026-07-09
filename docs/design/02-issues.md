# 02 — Phase 2 Issue Audit

Grounded in `01-findings.md`. Paths under `src/placement_mail_tracker/` abbreviated `pmt/`.

Baseline decisions confirmed by the user after Phase 1 and treated as fixed here:
1. **Auth**: third token file `config/calendar_token.json`, Calendar scope only; wrap `credentials.refresh()` + `RefreshError` alerting in **all three** token stacks.
2. **My Status**: write-back to SQLite in the sheets read-back path; calendar filtering reads the DB only.
3. **Date-NULLing update path** = BLOCKER with an **upstream** fix; the calendar layer must not compensate for it.

---

## Part A — Plan-vs-reality conflicts

### BLOCKER

**A1. `applied_only` mode assumed My Status is queryable from the DB — it is sheet-only.**
- Evidence: `pmt/sheets/sheets_sync.py:430-458` (`_read_my_status_map` reads the sheet into a per-sync dict); no code path writes it to `opportunities.my_status`, which stays `'NOT_APPLIED'` (`pmt/db/manager.py:192`, `:1058-1059`).
- Impact: `CALENDAR_SYNC_MODE=applied_only` for OA/INTERVIEW events would silently produce an empty calendar — the DB says nothing is applied.
- Resolution (decided): extend the sheets sync so the read-back map is persisted into `opportunities.my_status` (single batched `UPDATE … WHERE drive_id = ?` pass right after `_read_my_status_map`). Calendar module then filters on the DB column only. Ordering note: the sheets sync already runs **before** the calendar step in the pipeline (`pmt/scheduler/runner.py:627-659`), so within one run the calendar sees this run's fresh My Status values.

**A2. "Extend existing OAuth SCOPES, reuse token flow, one-time re-consent" — there is no single SCOPES list or shared token.**
- Evidence: Gmail stack `pmt/gmail/gmail_client.py:29` + `config/token.json` (`pmt/config/settings.py:28`); Sheets stack `pmt/sheets/sheets_sync.py:31` + `config/sheets_token.json` (`settings.py:52-55`). Independent `authenticate()` flows: `gmail_client.py:154-182`, `sheets_sync.py:581-606`.
- Impact: as written, the baseline is unimplementable — there is nothing to "extend". Widening the Sheets token's scopes would additionally invalidate a working token (Google returns `invalid_scope`/forces re-consent) and couples two failure domains.
- Resolution (decided): third per-service stack — `config/calendar_token.json`, scope `https://www.googleapis.com/auth/calendar` only, `CALENDAR_TOKEN_FILE` setting, same load/refresh/delete-on-corruption pattern copied from `sheets_sync.py:581-638`. One new interactive consent for Calendar only; Gmail/Sheets tokens untouched.

### HIGH

**A3. "Catch RefreshError/invalid_grant → specific SMTP alert" — no such hook exists anywhere today, and token death blocks scheduled runs interactively.**
- Evidence: `credentials.refresh(Request())` is unwrapped in both stacks (`pmt/gmail/gmail_client.py:164`, `pmt/sheets/sheets_sync.py:589`). `google.auth.exceptions.RefreshError` is not `HttpError` and not `GmailAuthenticationError`/`SheetsAuthenticationError`, so it escapes the targeted handlers (`gmail_client.py:235-246`, `sheets_sync.py:152-187`) and surfaces as a generic component failure. Worse, after the dead token is deleted, the next run launches `flow.run_local_server(port=0, timeout_seconds=120)` (`gmail_client.py:180`, `sheets_sync.py:604`) — an interactive browser flow on a headless scheduled run that hangs 120 s and fails, every 3 h, with only the generic streak alert after 3 failures.
- Impact: exactly the failure mode the baseline calls out (Testing-status 7-day token death) already exists for Gmail/Sheets and would exist for Calendar; the user learns about it via a vague "failure streak: 3" email at best.
- Resolution (decided): in all three `authenticate()` methods, wrap the refresh in `try/except RefreshError` → raise the stack's typed auth error with an "OAuth dead — re-consent needed for <service>" message; the calendar/sheets/gmail component handlers already convert typed errors into report failures, and a dedicated one-shot SMTP alert (dedup via `system_health.json`-style flag or `sent_alerts`-style key) tells the user which token to re-consent. Never auto-launch `run_local_server` when `sys.stdin`/env indicates a scheduled run — fail fast with the alert instead.

**A4. "start/end ISO8601 +05:30" vs reality: DB dates are free text parsed to *naive local* datetimes.**
- Evidence: date columns are TEXT (`pmt/db/manager.py:174-176, 193`); the canonical parser strips tz info to naive local (`pmt/utils/time.py:48-49, 104-105`); nothing in the repo mentions `+05:30` or `Asia/Kolkata`.
- Impact: `derive.py` cannot read structured datetimes; it must parse free text itself, and "attach +05:30" is a *new* convention — correct only while the machine's local tz is IST. A non-IST laptop tz (travel, VM) would shift every event.
- Resolution: `derive.py` parses via `parse_datetime_flexible` (per CLAUDE.md convention) and localizes with an explicit `CALENDAR_TIMEZONE` setting (default `Asia/Kolkata`, applied via stdlib `zoneinfo`) — never `datetime.astimezone()` machine defaults. `content_hash` is computed over the localized ISO strings so a tz-config change forces a clean re-PATCH.

### MEDIUM

**A5. "Feeds exit code 2 and the existing failure counter" works, but `RunReport` cannot represent a calendar component.**
- Evidence: `mark_component` only sets attributes that exist (`pmt/reliability/status.py:59-61`) — `calendar_ok` doesn't; `main._merge_report` hard-codes the four `*_ok` flags (`main.py:118-127`); `summary_lines` and `FailureAlertManager._build_alert_body` enumerate the same four (`status.py:133-151`, `pmt/reliability/health.py:128-154`).
- Impact: `mark_component("calendar", False, …, critical=False)` *does* append a warning → PARTIAL_SUCCESS → exit 2 → streak counter, so the baseline's contract technically holds — but the status line/alert body would never say "Calendar" failed, defeating months-later debuggability.
- Resolution: add `calendar_ok: bool = True` to `RunReport`, and extend `_merge_report`, `summary_lines`, and `_build_alert_body` symmetrically. Mechanical, test-covered by existing `test_reliability_status.py` patterns.

**A6. `UNIQUE(drive_id, event_type)` identity key rests on a non-unique, best-effort column.**
- Evidence: `opportunities.drive_id` has a plain (non-unique) index (`pmt/db/manager.py:264-265`); uniqueness is approximated by a `LIKE`-count suffix at insert (`manager.py:822-828`) and a second, slightly different backfill in `pmt/db/migrate.py:101-118`.
- Impact: two drives sharing a `drive_id` (possible via the LIKE-count race or historical backfill) would collide on the calendar identity key — one drive's events silently overwrite the other's.
- Resolution (baseline amendment, to be recorded in the ADR): identity key becomes `UNIQUE(opportunity_id, event_type)` with `FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE` — same FK pattern as `sent_alerts` (`manager.py:247-254`). Keep `drive_id` as a stored display/traceability column and in `extendedProperties.private` alongside it.

**A7. Baseline pipeline order names steps that don't exist where it says they are.**
- Evidence: actual order is Digest (before fetch, hour ≥ 8 — `pmt/scheduler/runner.py:234-242`) → Fetch → Extract/Store → Sheets → deadline/event Alerts (`runner.py:627-659`); heartbeat + failure counter live in `main._finalize_run` (`main.py:130-154`), not in the runner.
- Impact: minor — but "Calendar → Notify" must be read as "insert the calendar step between the Sheets block and the Alerts block inside `_execute_sync_pipelines`", and calendar anomalies flagged "in the digest" will surface in the *next morning's* digest, not the same run's.
- Resolution: insert calendar sync at `runner.py:650` (after the sheets `if not sheets_sync_successful` block, before the AlertGenerator block), wrapped in its own try/except so failure cannot block alerts. Digest latency for anomaly lines is accepted and documented.

### LOW

**A8. `--calendar-dry-run` / `--calendar-rebuild` are the repo's first CLI flags.**
- Evidence: `main.py:30, 157-158` — no argparse anywhere.
- Impact: none functionally; just new surface.
- Resolution: minimal `argparse` in `main.py` with the two flags threaded into `PlacementTrackerRunner` (or a dedicated short-circuit that runs only the calendar step for `--calendar-rebuild`). Task Scheduler invocation (`scripts/run_tracker.bat`) stays argument-less and unaffected.

**A9. "Same philosophy as the existing sheets sync" holds for source-of-truth, not for mechanism.**
- Evidence: sheets sync is clear-then-write per run with no persisted render state (`pmt/sheets/sheets_sync.py:318-353`).
- Impact: the calendar diff engine + `calendar_events` state table is the first *stateful* render target; there is no in-repo precedent to copy for the diff bookkeeping, only for auth/retry/row-builder seams.
- Resolution: none needed — noted so nobody "simplifies" the calendar to clear-and-write (deleting/recreating gcal events breaks event IDs, attendee state, and the NEVER-auto-delete rule).

---

## Part B — Existing-system weaknesses relevant to placement season

### BLOCKER

**B1. Follow-up emails silently NULL stored dates (`deadline`, `oa_date`, `interview_date`).**
- Evidence: `_update_opportunity_row` sets `deadline = :deadline`, `interview_date = :interview_date`, `oa_date = :oa_date` with **no COALESCE**, unlike the twelve COALESCE-guarded columns below them (`pmt/db/manager.py:923-926` vs `:931-945`). `_normalize_opportunity` always populates these keys (None when absent, `manager.py:1045-1071`), and any other changed field triggers the UPDATE. The erasure is invisible: `_changed_fields` only records changes where `new_value is not None` (`manager.py:1095`), so no `updates` row is written.
- Impact: the most common follow-up ("you are shortlisted", "OA link inside") rarely restates the registration deadline → the deadline vanishes from the DB. Sheets shows blank, deadline alerts stop firing, and the calendar's "date became null" rule would fire constantly for what is actually upstream data loss — during the exact weeks the data matters most.
- Resolution (decided — upstream fix, calendar must not compensate): guard the three date columns with `COALESCE(:deadline, deadline)` etc., matching the surrounding pattern. A genuine "deadline removed" email cannot be distinguished from an omission anyway, so dates only change via explicit new values. Add regression tests (see B8). After this fix, "date became null" in the calendar diff becomes the rare, legitimate signal the baseline designed for.

### HIGH

**B2. A transient Sheets read failure erases every user-entered My Status.**
- Evidence: `_read_my_status_map` returns `{}` on *any* exception (`pmt/sheets/sheets_sync.py:457-458`), and the sync proceeds to clear-and-write ALL DRIVES with the DB fallback "Not applied" (`sheets_sync.py:646-655`).
- Impact: one 503 on the read call and the user's entire application-tracking state — the input to MY APPLICATIONS, and (post-A1) to calendar `applied_only` filtering — is wiped. Today there is no recovery source.
- Resolution: two-part. (1) With A1's write-back, the DB becomes the durable copy: on read-back failure, fall back to DB `my_status` values instead of blanks — the wipe self-heals. (2) Distinguish "read failed" (exception → raise into the existing retry wrapper at `sheets_sync.py:152-187`) from "legitimately empty" (no rows → `{}`); never treat an API error as an empty sheet.

**B3. Alert dedup never re-arms after a reschedule.**
- Evidence: `sent_alerts` is keyed `UNIQUE(opportunity_id, alert_type)` forever (`pmt/db/manager.py:247-254`); `_should_send_alert`/`_mark_alert_sent` check only that pair (`pmt/scheduler/alert_generator.py:118-132`); alert types are static strings like `EVENT_24H`.
- Impact: OA on June 10 fires `EVENT_24H`; college reschedules to June 17 (the update path *does* store the new date); the June 17 OA gets **no** 24 h alert because `EVENT_24H` is already burned. Rescheduling is routine during season — this defeats the alert system precisely when calendars churn. The calendar PATCH will show the new date, but the push notification the user relies on is gone.
- Resolution: append the event date to the alert key (e.g. `EVENT_24H:2026-06-17`), or delete an opportunity's `sent_alerts` rows whose alert_type targets a changed date field inside `update_opportunity`. Either is a few lines; the date-suffixed key needs no schema change (the UNIQUE constraint still applies) and keeps history.

**B4. Fuzzy date parsing can mint plausible-looking garbage dates that would become real calendar events.**
- Evidence: `parse_datetime_flexible` calls dateutil with `fuzzy=True` (`pmt/utils/time.py:103`), which happily extracts a "date" from strings like "Round 3 at 5 in Lab 2". Gemini/rule-extracted date strings go into the DB unvalidated beyond a year-range check (`time.py:117-123`); `_warn_data_quality` only warns when a deadline is *un*parseable (`pmt/scheduler/runner.py:102-105`) — parseable garbage passes silently.
- Impact: today the blast radius is a wrong sheet cell or a stray alert; with calendar sync, a garbage date becomes a persistent, reminder-firing event on the user's phone.
- Resolution: the calendar boundary gets a stricter gate, without changing DB behaviour: `derive.py` re-parses raw strings with `fuzzy=False` (a thin `parse_event_datetime` wrapper in `utils/time.py`); strings that only parse fuzzily are excluded from event creation and flagged in the digest anomaly list. This respects decision 3's spirit — it is input validation at the calendar's trust boundary, not compensation for a DB bug.

### MEDIUM

**B5. Heartbeat starves on any warning, and calendar adds a new chronic warning source.**
- Evidence: `heartbeat_manager.update_success` runs only when `report.status == RunStatus.SUCCESS` (`main.py:139-140`); a single warning forces PARTIAL_SUCCESS (`pmt/reliability/status.py:100-102`); inactivity warnings then fire off the stale timestamp (`pmt/reliability/heartbeat.py:55-79`).
- Impact: a calendar token dying quietly (or any recurring warning) means the heartbeat never advances even though fetch/store/sheets are healthy → misleading "Tracker inactive for N hours" noise all season.
- Resolution: write the heartbeat on SUCCESS **and** PARTIAL_SUCCESS (recording the status in the payload), keeping only FAILED runs from refreshing it. `detect_inactivity` semantics ("the pipeline ran") stay honest; component failures are already covered by the streak alert.

**B6. Duplicate drives (dedup misses) would double-create calendar events.**
- Evidence: identity hash includes `role` (`pmt/db/manager.py:74-90`), so the same drive announced in a *new thread* with a differently-extracted role ("SDE Intern" vs "Software Development Engineer Intern") bypasses thread and hash matching; the last line of defence is fuzzy matching (`pmt/scheduler/runner.py:523-534`, rapidfuzz threshold in `pmt/utils/deduplication.py`), which is probabilistic.
- Impact: two `opportunities` rows for one real drive → two DEADLINE/OA events on the same day for the same company, plus double alerts. Users notice duplicated calendar entries far more than duplicated sheet rows.
- Resolution: `sync.py` adds a render-time collision check — if two *distinct* opportunity_ids derive events with the same `(normalized company, event_type, date)`, create only the first and flag the collision in the digest anomaly list. Upstream dedup tuning stays out of scope.

**B7. Date-only strings parse to midnight, which breaks "end < now → done" and reminder timing.**
- Evidence: `parse_datetime_flexible("15 June 2026")` → `2026-06-15 00:00` (strptime/dateutil default, `pmt/utils/time.py:21-30, 101-107`); most extracted deadlines are date-only.
- Impact: a deadline event placed at 00:00 IST is already "in the past" one minute into its own day — the freeze rule (`end < now → done`) would freeze it before the day begins, and a 24 h reminder fires at midnight the day before instead of a useful hour.
- Resolution: `derive.py` detects time-of-day presence in the *raw string* (regex for `:`/am/pm/hrs) since the parsed datetime can't distinguish "midnight" from "no time". Date-only DEADLINE events become **all-day events** (Calendar API `date`, not `dateTime`) with end = the deadline date, treated as ending 23:59 local for diff purposes; date-only OA/INTERVIEW events become all-day with the standard reminder offsets.

**B8. Zero test coverage on the date-reschedule update path the calendar depends on.**
- Evidence: `tests/test_followup_detection.py:32-248` covers thread matching, status-history accumulation, drive-ID format, and company normalization — no test changes `oa_date`/`deadline`/`interview_date` on a follow-up and asserts the row + `updates` history; no test covers the B1 NULLing behaviour.
- Impact: the exact contract `sync.py` diffs against ("updated rows actually change in the DB, erasures don't happen") is unpinned; the B1 fix could regress silently.
- Resolution: add to the suite (spec'd in `04-integration-spec.md`): (1) follow-up with a new `oa_date` → row updated, `updates` row written, `updated_at` bumped; (2) follow-up omitting `oa_date` → stored value preserved (post-B1); (3) follow-up with changed deadline re-arms alerts (post-B3).

### LOW

**B9. The daily digest runs before fetch, on yesterday's data.**
- Evidence: `_run_daily_digest` is called before `_fetch_messages` (`pmt/scheduler/runner.py:196-203`).
- Impact: digest content (and future calendar-anomaly lines) lags one cycle (~3 h). Tolerable; noted so the calendar design doesn't promise same-run digest visibility.
- Resolution: accept and document; if it ever matters, moving the digest call after `_execute_sync_pipelines` is a one-line reorder with test updates.

**B10. `updates.created_at` is UTC ISO while event dates are naive local.**
- Evidence: `create_update_event` stamps `utc_now_iso()` (`pmt/db/manager.py:495`); event dates are naive local free text (finding c).
- Impact: none for the calendar design (it never compares the two), but anyone extending the diff to "recently changed rows only" must not mix the clocks.
- Resolution: no change; recorded as a constraint for `sync.py` (diff by content hash, never by timestamp comparison).
