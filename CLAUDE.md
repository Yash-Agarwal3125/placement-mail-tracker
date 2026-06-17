# CLAUDE.md — Operating Guide for the Placement Mail Tracker

This file orients any engineer (human or AI) working in this repository.

## What this is

A **zero-touch, one-shot batch job** that a Windows Task Scheduler runs every few
hours. Each run: reads recent Gmail → filters placement/internship mail →
extracts structured fields (rules first, Gemini only when needed) → stores
drive-centric records in SQLite → syncs active drives to Google Sheets → sends
email digests/alerts → records health/heartbeat state.

It must survive being run unattended for months on a personal machine.

## Operating principles (in priority order)

1. **Simplicity over complexity.** Prefer the boring, obvious solution.
2. **Maintainability over cleverness.**
3. **Deterministic logic over AI calls.** Regex/rules are the default path.
4. **Gemini is the exception, not the default.** Every Gemini call costs money;
   only call it when rule-based extraction is genuinely insufficient
   (`RuleExtractionResult.needs_gemini`).
5. **Optimise for long, unsupervised operation.** Fail soft, log clearly,
   never wedge.
6. **Minimise operational cost** (Gemini tokens, Sheets API calls, disk I/O).
7. **Preserve existing behaviour** unless there is a compelling, justified reason
   to change it. Favour incremental change over rewrites.
8. **Every behavioural change keeps the test suite green** (`python -m pytest`).

## Architecture map

```
main.py                      entry point: settings → logging → validate → lock → run
scheduler/runner.py          PlacementTrackerRunner.run_once(): the pipeline
gmail/gmail_client.py        Gmail OAuth + fetch (gmail/client.py is a re-export shim)
gmail/filters.py             is_placement_mail() relevance scoring
extraction/rule_engine.py    regex extraction, classification, status detection
extraction/eligibility.py    branch/degree/CGPA eligibility vs UserProfile
ai/gemini_extractor.py       Gemini fallback extraction (+ ai/models.py schema)
utils/deduplication.py       fuzzy duplicate detection (rapidfuzz)
utils/scoring.py             priority (HIGH/MEDIUM/LOW)
db/manager.py                DatabaseManager: the real schema + all queries
db/connection.py             sqlite connection factory (WAL)
sheets/sheets_sync.py        Google Sheets sync + formatting
scheduler/digest_generator.py / alert_generator.py  email digests + deadline alerts
reliability/                 status report, health (failure streak), heartbeat
utils/lock_manager.py        single-instance lock (PID liveness)
```

## Data model (drive-centric)

`opportunities` rows ARE drives, not emails. A follow-up email updates the
existing drive instead of inserting a new row. Matching order:
1. Gmail `thread_id` (strongest signal),
2. content hash (`company + role + package + year`),
3. fuzzy match (rapidfuzz) on company/role/type.

Two status axes exist: `status` (row lifecycle, effectively always `active`) and
`current_status` (`OPEN → REGISTERED → … → OFFER_RECEIVED/REJECTED`). Date
fields (`deadline`, `oa_date`, `interview_date`, `next_event_date`) are stored as
free text and parsed with `utils.time.parse_datetime_flexible` — never assume ISO.

## Conventions

- Always parse extracted dates with `parse_datetime_flexible`, not
  `datetime.fromisoformat` (extracted dates are rarely ISO).
- The Gmail fetch window (`data/fetch_state.json`) only advances when the fetch
  **succeeds**, so a transient Gmail outage never silently drops emails.
- Keep per-email logging at `DEBUG`; one concise `INFO` summary per email is enough.
- `ACTIVE_OPP_HEADERS` defines the Active/Filtered sheet layout and is asserted by
  tests — change with care. The sheet is decision-first and human-readable:
  dates are rendered in local time as text (forced via a leading apostrophe in
  `_force_text` so Sheets doesn't re-localise them), enums get friendly labels,
  and the internal `Drive ID` is the **last** column (`ACTIVE_KEY_INDEX`), used
  as the row dedupe key.
- `My Status` is **user-owned** (`ACTIVE_USER_COLUMNS`): the sync preserves the
  value already in the sheet and must never overwrite it from the DB.
- Eligibility filtering needs `config/user_profile.json`; `UserProfile.load`
  warns and falls back to a default if it's missing.

## Running

```powershell
python main.py                 # one sync cycle
python -m pytest               # full suite (must stay green)
ruff check .                   # lint (CI runs this)
```
