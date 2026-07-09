# 03 — ADR: Google Calendar Sync for Placement Drives

Status: **Proposed** (design approved through Phase 2; no code written).
Grounding: every file:line citation below was verified in `01-findings.md`;
issue IDs (A1…B10) refer to `02-issues.md`.

---

## Context

The tracker is a zero-touch 3-hourly batch job. Drives live in the
`opportunities` table (`pmt/db/manager.py:163-199`) with three free-text event
date columns — `deadline`, `oa_date`, `interview_date` — parsed everywhere via
`parse_datetime_flexible` (`pmt/utils/time.py:86`). We want a dedicated
"VIT Placements" Google Calendar kept in sync with those dates, including
PATCHes when the college reschedules, surviving months of unsupervised runs.

Constraints that shaped this ADR:

- The DB is the source of truth; the calendar is a render target. But unlike
  the sheets sync (clear-then-write, stateless — `pmt/sheets/sheets_sync.py:318-353`),
  a calendar cannot be cleared and rewritten: Google event IDs, phone
  notifications, and the NEVER-auto-delete rule all require *stateful* diffing.
- Dates are naive-local free text; `My Status` was sheet-only until this
  design (A1); auth is per-service tokens, not one shared token (A2).
- Calendar failure must never block sheets/alerts and must feed exit code 2
  and the failure-streak counter (`pmt/reliability/status.py:84-112`,
  `pmt/reliability/health.py:96-116`).

## Decision (summary)

Add `pmt/calendar_sync/` (`client.py`, `derive.py`, `sync.py`) plus a
`calendar_events` state table created by `DatabaseManager.create_tables`.
Each active drive derives up to three events (DEADLINE / OA / INTERVIEW).
A content-hash diff decides insert / PATCH / no-op per event; terminal and
vanished drives get retitles, never deletes. Auth is a third OAuth token
(`config/calendar_token.json`) with the Calendar scope only. The step runs in
`_execute_sync_pipelines` between the Sheets block and the AlertGenerator
block (`pmt/scheduler/runner.py:650`), isolated in its own try/except, and is
reported via a new `RunReport.calendar_ok` flag (never critical).

The full implementation contract (DDL, signatures, config, tests) is in
`04-integration-spec.md`. The rest of this ADR records each major decision
with alternatives and consequences.

---

## Decision 1 — Identity key: `UNIQUE(opportunity_id, event_type)` *(amends baseline)*

**Chosen:** `calendar_events` is keyed by `UNIQUE(opportunity_id, event_type)`
with `FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE
CASCADE`. `drive_id` is stored as a display/traceability column and written
into the Google event's `extendedProperties.private` (alongside
`opportunity_id`), but carries no uniqueness burden.

**Alternatives considered:**
1. *Baseline: `UNIQUE(drive_id, event_type)`.* Rejected with evidence:
   `opportunities.drive_id` has only a plain index (`manager.py:264-265`) and
   its uniqueness is a best-effort `LIKE`-count suffix at insert
   (`manager.py:822-828`) plus a slightly different backfill in
   `pmt/db/migrate.py:101-118`. A drive_id collision would silently cross-wire
   two drives' events (issue A6).
2. *No local table — look events up on Google by
   `extendedProperties.private.drive_id`.* Rejected: one search API call per
   drive per run (cost principle #6), and it makes the network the source of
   record for sync state, violating "DB is source of truth" and making
   `--calendar-dry-run` impossible offline.

**Consequences:** `ON DELETE CASCADE` matches the existing `sent_alerts`
pattern (`manager.py:247-254`). If a duplicate drive is ever merged/deleted,
its event state rows vanish with it (the gcal events themselves are then
orphans handled by the stale pass). `extendedProperties` still lets a human —
or a future rebuild — trace any Google event back to its drive.

**Revisit when:** drives ever get a DB-enforced unique key, or a "merge
duplicate drives" feature lands (it must then re-point or retire event rows).

## Decision 2 — Diff algorithm: stored content-hash, PATCH by stored event ID

**Chosen:** per sync, `derive.py` builds the *desired* event list from
`fetch_active_drives_only()` rows (`manager.py:544-566`) filtered by
eligibility (`eligibility_status` not `NOT_ELIGIBLE_*`, same rule as the sheet
at `sheets_sync.py:205-208`). For each desired event, compare
`content_hash = sha256(start|end|title|location)` against the stored row:

| Condition | Action |
|---|---|
| no `calendar_events` row | insert Google event → store `gcal_event_id`, hash, `status='active'` |
| row exists, hash equal | no-op (zero API calls) |
| row exists, hash differs | PATCH by stored `gcal_event_id` — never search-by-title |
| event end < now | mark row `done`; frozen — excluded from all future diffing |
| drive still active but its date became NULL | keep the Google event untouched, flag in digest anomalies (post-B1 this is rare and means something) |
| drive absent from active set (terminal `current_status` or vanished) | grace period (`CALENDAR_STALE_AFTER_HOURS`, default 48 h tracked via `last_seen_active_at`), then one PATCH retitling to `[?] <old title>` and mark `stale` (vanished) / `cancelled` (terminal). **NEVER delete.** |
| active-drive query returns zero rows | abort the stale pass entirely (partial-fetch guard) — insert/patch for any derived events still runs, retitles do not |

**Alternatives considered:**
1. *Clear-and-rewrite like the sheets sync.* Rejected: deleting and recreating
   events changes event IDs every run, re-fires phone notifications, destroys
   any user edits on events, and violates NEVER-auto-delete.
2. *Timestamp diff — re-render only drives with `updated_at > last_sync`.*
   Rejected: `updated_at`/`updates.created_at` are UTC ISO while event dates
   are naive local (B10); a crash between calendar write and state write would
   permanently skip changes; manual DB edits would never sync. The hash
   compare over all active drives is O(active drives) with zero API calls for
   unchanged events — cheap and self-healing.
3. *Diff against the live calendar (list events, compare).* Rejected: an API
   list call every run for the common all-unchanged case; and a user edit on
   an event (adding notes) would look like drift and get clobbered.

**Consequences:** the diff is idempotent and crash-safe in one direction only:
state rows are written **after** the Google call succeeds, so a crash between
them causes a duplicate insert attempt next run. Mitigation: on insert, the
engine first consults the stored row; only a row with NULL `gcal_event_id`
can re-insert, and inserts set the row in the same transaction as the
response. Residual risk (crash between API success and commit) is accepted —
worst case one duplicate event, visible and manually deletable, flagged by
the B6 collision check on later runs.

**Revisit when:** active-drive count grows enough that per-run derive cost
matters (hundreds of drives — not this project), or Google adds batch PATCH.

## Decision 3 — Dates at the boundary: strict parse, explicit IST, all-day fallback *(amends baseline)*

**Chosen:** `derive.py` parses the raw DB strings with a new *strict* wrapper
(`fuzzy=False`) rather than the permissive `parse_datetime_flexible`
(`fuzzy=True` at `time.py:103`); strings that parse only fuzzily produce **no
event** and a digest anomaly (B4). Parsed naive datetimes are localized with
`zoneinfo.ZoneInfo(settings.calendar_timezone)` (default `Asia/Kolkata`) —
never the machine default (A4). Raw strings with no time-of-day token
(regex on the raw string for `:` / am/pm / hrs) become **all-day events**
(Calendar API `date` form); for diff purposes a date-only deadline "ends"
23:59 local so the done-freeze rule doesn't fire at 00:01 on the deadline day
(B7). Timed events: OA/INTERVIEW get a 1 h default duration, DEADLINE a
30 min block ending at the deadline instant.

**Alternatives considered:**
1. *Baseline: store/compare `start/end ISO8601 +05:30` from structured DB
   datetimes.* Amended because no structured datetimes exist — columns are
   free text and the shared parser strips timezones to naive local
   (`time.py:48-49, 104-105`).
2. *Reuse `parse_datetime_flexible` as-is (CLAUDE.md convention).* Rejected
   for this one boundary: fuzzy parsing turning "Round 3 at 5 in Lab 2" into a
   real phone-notifying calendar event is worse than a missing event. This is
   input validation at a trust boundary, not a convention violation — the DB
   and sheet keep the flexible parser.
3. *Fix timestamps upstream (normalize dates to ISO+tz at extraction time).*
   Rejected: a rewrite of extraction/storage semantics with sheet-format
   blast radius, contra operating principle 7 (preserve existing behaviour).

**Consequences:** `content_hash` covers the localized ISO strings, so
changing `CALENDAR_TIMEZONE` forces a clean re-PATCH of every event —
correct behaviour. Some sloppily-worded but real dates will be excluded
until a follow-up email restates them; the anomaly line tells the user.

**Revisit when:** extraction ever starts emitting normalized ISO dates, or
anomaly lines show the strict parser rejecting too many real dates.

## Decision 4 — Scope & auth: third token file, wrapped refresh everywhere *(amends baseline)*

**Chosen (user-confirmed):** a third per-service OAuth stack:
`config/calendar_token.json`, scope `https://www.googleapis.com/auth/calendar`
only, `CALENDAR_TOKEN_FILE` setting, copying the Sheets pattern
(`sheets_sync.py:581-638`) including delete-corrupted-token auto-healing.
Additionally — because the promised RefreshError alert has no existing hook —
`credentials.refresh(Request())` gets wrapped in **all three** stacks
(`gmail_client.py:164`, `sheets_sync.py:589`, new calendar client):
`RefreshError`/`invalid_grant` → the stack's typed auth error carrying
"OAuth dead — re-consent needed for <service>" → component failure + one-shot
SMTP alert. The interactive `run_local_server` flow is never auto-launched on
a scheduled (non-TTY) run; it fails fast with the alert instead of hanging
120 s (`gmail_client.py:180`, `sheets_sync.py:604`; issue A3). Operational
prerequisite: the GCP OAuth app must be in "In production" publishing status
(Testing = 7-day refresh-token death).

**Alternatives considered:**
1. *Baseline: extend "the" SCOPES list, reuse "the" token.* Unimplementable
   as written — two independent token stacks exist, no shared SCOPES (A2).
2. *Widen the Sheets token to `spreadsheets + calendar`.* Rejected: forces
   re-consent of a currently working token, and couples two failure domains —
   a Calendar-related token revocation would take the Sheets sync down with it.
3. *One combined token for Gmail+Sheets+Calendar.* Rejected for the same
   coupling reason, times three, plus a mass re-consent migration.

**Consequences:** one more interactive consent at rollout; three token files
to babysit — mitigated by the new per-service OAuth-dead alert that names the
service. No change to Gmail/Sheets behaviour except strictly better error
reporting on refresh death.

**Revisit when:** Google Identity consolidates incremental scope grants for
installed apps, or the project moves to a service account.

## Decision 5 — Failure isolation & reporting: own try/except + `calendar_ok`, never critical

**Chosen:** the calendar step runs inside `_execute_sync_pipelines` after the
Sheets block and before the AlertGenerator block (`runner.py:650`), gated on
`settings.calendar_sync_enabled`, wrapped in its own try/except exactly like
sheets/alerts. On failure: `report.mark_component("calendar", False, msg,
critical=False)` — **always** non-critical, even in production (unlike sheets,
which is critical in prod at `runner.py:643-649`): the calendar is enrichment;
mail ingestion and the sheet must survive its death. To make that reportable,
`RunReport` gains `calendar_ok: bool = True` and the three hard-coded
enumerations are extended symmetrically: `main._merge_report`
(`main.py:118-127`), `summary_lines` (`status.py:133-151`), and
`FailureAlertManager._build_alert_body` (`health.py:128-154`) — issue A5.
A failed calendar → warning → `PARTIAL_SUCCESS` → exit 2 → existing streak
counter, all with zero changes to that machinery.

**Alternatives considered:**
1. *Warnings-only (no `calendar_ok` field).* Works mechanically —
   `mark_component` skips unknown attrs but still appends the warning
   (`status.py:59-70`) — but the status summary and streak-alert email would
   never say "Calendar OK: False", hurting months-later debuggability.
2. *Critical in production, mirroring sheets.* Rejected: a dead calendar
   token would mark whole runs FAILED, blocking heartbeat updates entirely
   and drowning the season in inactivity alarms (see B5).

**Consequences:** with B5's companion fix (heartbeat written on
PARTIAL_SUCCESS too), a chronically failing calendar degrades loudly (streak
alert names the component) without ever costing an email or a sheet row.

**Revisit when:** the user starts treating the calendar as primary (then
promote to critical-in-production consciously).

## Decision 6 — `applied_only` filtering: DB-only, fed by My Status write-back *(amends baseline mechanism, not behaviour)*

**Chosen (user-confirmed):** the sheets sync persists its read-back map into
`opportunities.my_status` (a batched UPDATE keyed by `drive_id`, right after
`_read_my_status_map` at `sheets_sync.py:211`). The calendar module reads the
DB only — it never touches the Sheets API. Mode semantics: DEADLINE events
are created for all visible eligible drives regardless of mode (you need the
deadline *before* you apply); `CALENDAR_SYNC_MODE` governs OA/INTERVIEW
events — `applied_only` (default) requires `my_status` ∉ {`NOT_APPLIED`,
NULL}; `all_eligible` includes them for every visible drive. Companion fix
B2: a failed read-back now falls back to DB values instead of writing blanks,
making the DB the durable copy of user intent.

**Alternatives considered:**
1. *Calendar module does its own Sheets read.* Rejected: a second Sheets API
   dependency inside the calendar failure domain, duplicate read cost every
   run, and a parsing contract on sheet layout in a second place.
2. *Keep My Status sheet-only and pass the in-memory map from the sheets step
   to the calendar step.* Rejected: creates a hidden ordering dependency
   (calendar silently empty of OA events when sheets fails) and leaves B2's
   wipe-on-error hole open.

**Consequences:** `my_status` finally means something in the DB (it was a
dead column defaulting to `NOT_APPLIED` — `manager.py:1058-1059`); MY
APPLICATIONS filtering could later read the DB too. One new write batch per
sync (only for changed values — skip no-ops to respect API/disk cost).

**Revisit when:** the user wants to edit My Status from anywhere other than
the sheet (CLI, Telegram) — the DB column is then already the join point.

## Decision 7 — Dry-run and rebuild semantics

**Chosen:**
- `--calendar-dry-run` (built **first**, per baseline): runs derive + diff
  fully, prints the planned operations (insert/patch/done/stale/flagged, with
  titles and dates) and writes **nothing** — no Google calls, no
  `calendar_events` rows. Implemented as a `dry_run=True` flag on the sync
  engine so it exercises the real code path, not a parallel one.
- `--calendar-rebuild`: reconciliation, not recreation. For every
  `calendar_events` row: GET the stored `gcal_event_id`; 404/missing → clear
  the stored ID and re-insert; present but hash-mismatched → PATCH. Rows are
  never deleted; Google events are never deleted. Use case: user manually
  deleted events, or the state table was restored from backup.
- Both flags arrive via the repo's first `argparse` in `main.py` (A8);
  `scripts/run_tracker.bat` stays argument-less.

**Alternatives considered:**
1. *Rebuild = wipe the "VIT Placements" calendar and recreate.* Rejected:
   violates NEVER-delete, re-fires every notification, and orphans nothing
   recoverable if it crashes midway.
2. *Rebuild = `DELETE FROM calendar_events` and let the normal sync re-insert.*
   Rejected: the sync would insert *duplicates* of every still-existing Google
   event (the IDs were the only link).

**Consequences:** rebuild costs one GET per tracked event — acceptable for a
manual, occasional command. Dry-run output goes to the logger at INFO, so it
also works under the scheduler for a one-off supervised cycle.

**Revisit when:** never, realistically; these are maintenance tools.

## Decision 8 — Upstream fixes shipped with (but before) the calendar *(records Phase 2 BLOCKER resolution)*

Per the user's Phase 2 decisions, these land as prerequisite PR-sized changes,
each keeping `python -m pytest` green, **before** the calendar module:

1. **B1 (BLOCKER):** COALESCE-guard `deadline`, `interview_date`, `oa_date` in
   `_update_opportunity_row` (`manager.py:923-926`), matching the surrounding
   columns — follow-ups can no longer NULL stored dates. The calendar layer
   does not compensate; its "date became null" rule stays rare-and-meaningful.
2. **A1/B2:** My Status write-back + read-back hardening (Decision 6).
3. **A3:** wrapped refresh + OAuth-dead alert in all three stacks (Decision 4).
4. **B3:** re-arm alerts on reschedule by date-suffixing the `sent_alerts`
   alert_type (e.g. `EVENT_24H:2026-06-17`) — no schema change.
5. **A5:** `RunReport.calendar_ok` + report plumbing (Decision 5).
6. **B5:** heartbeat written on PARTIAL_SUCCESS as well as SUCCESS
   (`main.py:139-140`), payload records the status.

## Consequences (overall)

- New steady-state cost per 3 h run with nothing changed: **zero** Calendar
  API write calls, one calendarList lookup (cacheable), one SELECT over
  active drives — consistent with cost principle #6.
- New failure surface is fully contained: worst case is warnings + exit 2 +
  a streak alert that names "Calendar".
- The `calendar_events` table follows the existing migration mechanism
  exactly (idempotent DDL in `create_tables` — `manager.py:149-280`), so
  fresh installs and existing DBs need no manual step.
- Baseline decisions amended (each with evidence, per scope rules): identity
  key (Decision 1), date/timezone handling (Decision 3), auth mechanism
  (Decision 4), My Status source (Decision 6). All other baseline decisions
  are confirmed unchanged: module layout `calendar_sync/` (stdlib-shadow
  avoidance), DB-as-source-of-truth, diff rules including grace-period
  retitle and partial-fetch guard, pipeline position, exit-code-2 feeding,
  config surface, dry-run-first.

## Revisit-when (consolidated)

- Drives gain a DB-enforced unique business key → reconsider identity key.
- Extraction emits normalized ISO dates → drop the strict-parse boundary.
- Calendar becomes primary UI → reconsider criticality and alert routing.
- Google offers incremental scope grants for installed apps → reconsider
  single-token auth.
