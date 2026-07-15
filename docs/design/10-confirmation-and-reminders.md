# 10 — Application-Confirmation Handling + Reminder Mails

Implementation doc for two features built against locked design decisions
from `docs/design/08-confirmation-audit.md` (Feature 1) and
`docs/design/05-improvement-backlog.md` items 1–2 (Feature 2). Numbered 10,
not 9, to avoid colliding with the existing `09-manager-review.md`.

---

## Feature 1 — Application-confirmation handling

### D4 ladder refactor (shipped first, both features + the sheet path depend on it)

`db/manager.py`: `bulk_update_my_status` is now a thin loop over a new
`set_my_status(drive_id, my_status, *, source="sheet")`, the single choke
point for every `my_status` write.

- `source="sheet"` (default): unrestricted, including downgrades — byte-for-
  byte the pre-existing behaviour. The one existing call site
  (`sheets_sync.py:229`) is unchanged.
- `source="automation"`: upgrade-only ladder —
  `NOT_APPLIED(0) → APPLIED(1) → SHORTLISTED(2) → SELECTED(3)/REJECTED(3)`.
  A write is applied only if it strictly advances rank; equal-or-higher is a
  no-op. This makes a duplicate or late confirmation mail idempotent for
  free (D6) — no content-level dedup table needed.

Tests: `tests/test_my_status_writeback.py::TestSetMyStatusLadder`.

### Detection (`extraction/confirmation.py`)

- **Sender gate (D1, hard)**: exact match on
  `noreply.cdcinfo@vitstudent.ac.in`. Nothing else can produce
  `APPLICATION_CONFIRMATION`. Implemented as the *first* check inside
  `classify_email()` (`rule_engine.py`), before the ordered pattern list that
  contains `OFFER_UPDATE`'s bare `"congratulations"` pattern — a
  confirmation phrased "Congratulations, your application has been
  submitted" never reaches that pattern at all, so the collision the audit
  flagged (C1) cannot happen by construction, not by pattern ordering.
- **CONFIRMED-tier pattern families** (named so real-mail logs show which
  family matched):
  - `successfully_applied_or_registered`
  - `application_or_registration_received`
  - `thank_you_for_applying`
  - `you_have_applied`
- **UNKNOWN tier**: sender matches, no family does. Still classified
  `APPLICATION_CONFIRMATION` (the sender is confirmation-only by definition)
  but never writes status, in either mode — the escape valve for phrasing
  nobody predicted.
- HTML-tolerant: tags (and `<style>`/`<script>` element bodies) are stripped
  before matching, and truncation to the working window happens *after*
  stripping, not before — an early draft of `find_confident_drive_match` had
  this backwards (truncate-then-strip), which could slice off all real
  content behind a long `<style>` preamble like the IBM Cloud fixture in
  `scripts/eval/corpus/`. Caught before landing; regression test:
  `test_html_preamble_longer_than_truncation_window_still_matches`.

All fixtures in `tests/test_confirmation_detection.py` are labeled
**SYNTHETIC** — zero real samples exist (audit blocker 1).

### D2 — explicit filter allow rule

`gmail/filters.py::calculate_relevance_score` short-circuits to
`is_placement=True` on an exact match of the confirmation sender, before any
keyword scoring runs. The audit (A2) predicted the relaxed-path substring
match and trusted-sender auto-discovery would *also* let this sender
through — deliberately not relied on, since that prediction was never
tested against a real subject line that could trip a negative/newsletter
keyword.

### D3 — my_status ONLY, nothing else touched

`_handle_confirmation_mail` (new method on `PlacementTrackerRunner`) is an
entirely separate branch in `_process_single_message`, checked immediately
after classification — a confirmation mail **never** reaches `rule_extract`,
Gemini, `insert_or_update_opportunity`, or the existing
`current_status="REGISTERED"` detection (`rule_engine.py:275-279`,
untouched). It can only ever call `set_my_status(..., source="automation")`.
Verified directly: `tests/test_confirmation_integration.py::
test_confirmation_never_creates_a_drive_or_touches_current_status`.

### Matching

1. **Reference-ID exact match** (`extract_reference_id` + `drive_id`
   comparison): implemented per spec, but **expect this to be permanently
   inert today**. No CDC-side reference/registration number is stored
   anywhere on `opportunities` (audit blocker 3) — `drive_id` is this
   system's own internal slug, not something CDC would ever echo back.
   There is nothing comparable for the extracted ID to match against until
   a real sample reveals CDC's actual ID format and a new column is added
   to store it. This is not a bug; it's a placeholder for a future fix.
2. **Fuzzy company match** (`rapidfuzz.fuzz.partial_ratio` against active
   drives' company names): threshold **≥90** (vs. the 60% floor used for
   sheet display, `MIN_PRECISION["company"]` in the eval harness) plus a
   **uniqueness margin of 5 points** — reject if the top two candidates are
   within 5 points of each other. An automatic status write needs a much
   higher, harder-to-notice-if-wrong bar than a sheet row a human reviews
   anyway.
3. **No confident match** (either method fails): the mail is persisted to a
   new `unmatched_confirmations` table (`message_id, extracted_text,
   candidates JSON, created_at`) and surfaced once in the digest — never
   silent-dropped.

### CONFIRMATION_MODE (default OFF)

`Settings.confirmation_mode: "observe" | "enforce"` (default `"observe"`).
In observe mode the full pipeline runs — tier detection, matching, corpus
capture, digest lines — but the final `set_my_status` call is skipped and
replaced with a "would have marked ⟨company⟩ APPLIED" log + digest line.
Flipping to `enforce` is a `.env` change, not a code change.

### Auto-captured eval corpus

Every classified `APPLICATION_CONFIRMATION` mail (either mode, either tier)
is written to `scripts/eval/corpus/confirmations/<message_id>.json`
(`scheduler/confirmation_corpus.py`), plus a `field="classification"` row
appended to `scripts/eval/labels.csv` with `prefill_label` set to the
detected tier and `corrected_label` left blank for a human to fill in once
real samples exist. Per the audit's C5 decision, **no scoring or
precision/recall floor is wired up for this field** — retrofitting
classification scoring for all 8 classification types is explicitly
off-season backlog, not part of this session. "Sanitized" here is limited to
stripping tracking-pixel `<img>` tags from the HTML body; these are the
user's own mails about their own applications, not another student's data,
so there is no real PII exposure to redact beyond that.

### Digest section

`CONFIRMATIONS` — one line per confirmation mail processed this run (tier,
match result, action taken or would-have-taken), backed by
`scheduler/confirmation_digest_store.py` (same JSON-file pattern as
`calendar_flags_store.py`).

### First real sample (2026-07-13 validation)

One real confirmation mail arrived: "Congratulations! Your TCS NQT
Application Has Been Successfully Submitted", sender `VIT - Soft Skill
Assessments <noreply.cdcinfo@vitstudent.ac.in>`, auto-captured by the
pipeline itself during its 2026-07-11 run
(`scripts/eval/corpus/confirmations/19f508fea985dbe7.json`, since enriched
with the full fetched HTML/headers). Two real gaps found and fixed against
this evidence, not guesses:

1. **All 4 CONFIRMED-tier families missed it** (landed UNKNOWN). Real CDC-
   adjacent copy inserts the qualifier/test name and adverbs between the
   anchor word and the verb — *"Your application for the TCS National
   Qualifier Test (NQT) has been **successfully** submitted"* — which the
   original tight adjacency pattern
   (`(application|registration)\s*(?:has\s*been\s*)?(received|submitted|confirmed)`)
   never allowed for. Broadened to
   `(application|registration)\b.{0,100}?\b(received|submitted|confirmed)\b`
   (lazy, bounded gap). Re-validated: now CONFIRMED via
   `application_or_registration_received`.
2. **Matching would have failed anyway, for an unrelated reason.** Against
   the real active-drive list, "TCS" and "SES" tied at
   `partial_ratio`-score 100 — not a coincidence of similar names, but
   because the literal 3-letter string "ses" is a substring of
   "as**ses**sments" in the body. `partial_ratio` aligns a short needle
   against *any* same-length window in the haystack, ignoring word
   boundaries, so short company names are structurally prone to this. The
   existing uniqueness-margin safety check correctly refused to guess
   between the tied candidates — but that meant the real correct match
   (TCS) was blocked by a false collision, not a genuine ambiguity. Fixed:
   company names of length ≤4 now require an actual whole-word regex match
   (`(?<![a-z0-9])name(?![a-z0-9])`) instead of `partial_ratio`; longer
   names are unaffected. Re-validated: TCS now matches uniquely (SES scores
   0, no longer a tied candidate).
3. **No reference/drive ID present** in this sample (plain text or raw
   HTML) — confirms audit blocker 3. No ID-extraction pattern was designed
   from it, since there's nothing to design from; the existing generic
   placeholder regex remains untested against a real ID format.

Regression tests locking both fixes in against this exact real sample:
`tests/test_confirmation_detection.py::TestRealSampleTcsNqt`,
`TestShortCompanyNameMatching`.

### Enforce-mode flip checklist (execute manually, later)

1. At least 2–3 REAL confirmation mails have arrived and were
   auto-captured under `scripts/eval/corpus/confirmations/`.
2. For each: check the digest recorded the correct tier, the correct drive
   match, and "would have marked APPLIED" points at the right drive.
3. If any real mail landed in UNKNOWN tier: add its phrasing as a new named
   pattern family, add it as an eval fixture, rerun tests.
4. If any real mail mismatched a drive: revisit the threshold/ID-extraction
   before flipping.
5. Flip `CONFIRMATION_MODE=enforce`. Optionally run the backfill:
   `python scripts/backfill_confirmations.py [--dry-run]`
   (`scripts/backfill_confirmations.py`, added 2026-07-15). Re-runs detection
   and matching against every fixture captured under
   `scripts/eval/corpus/confirmations/*.json` using *current* code (not the
   tier recorded at capture time, so a later pattern-family fix also benefits
   mail captured before it landed), then applies the same
   `set_my_status(..., source="automation")` ladder write the live enforce
   path would have. UNKNOWN tier and no-confident-match fixtures are skipped,
   never force-written. Idempotent: the ladder write is upgrade-only, so
   re-running the script after it already ran is a no-op for anything already
   applied. Tests: `tests/test_confirmation_backfill.py`.

---

## Feature 2 — Reminder mails from tracked data

**Supersedes last session's per-drive `DEADLINE_ESCALATION_48H`/`24H` alerts**
(added in the manager-review fix-list session, `docs/design/09-manager-
review.md` action #2/backlog promotion) — same feature, replaced with this
session's batched, locked design. The alert types are renamed
`DEADLINE_T48`/`DEADLINE_T24` and the send is now batched.

### Deadline escalation

- Selection (unchanged criterion): `eligibility_status == "ELIGIBLE"` and
  `my_status == "NOT_APPLIED"` and a parseable future deadline within the
  window (default thresholds `[48, 24]` hours,
  `settings.deadline_escalation_thresholds_hours`).
- **New exclusion**: a drive whose `validation_flags` contains a
  `deadline `-prefixed flag (the validation layer already distrusts this
  date, `extraction/validation.py`) is excluded from escalation and instead
  listed once in the digest under `DEADLINE UNVERIFIED — CHECK MANUALLY`.
- **Batching, not just dedup-loosening**: `sent_alerts` keeps exactly the
  same shape — one row per `(opportunity_id, alert_type)` — so per-drive
  re-arm-on-reschedule is unaffected. What changed is the *send*: all drives
  crossing the same threshold in the same run are collected first
  (`_collect_deadline_escalation_candidate`), then sent as **one** HTML mail
  per alert_type (`_send_batched_deadline_escalations`), sorted by
  soonest-first, capped at `settings.reminder_max_per_mail` (default 20).
  Overflow beyond the cap is noted ("+N more") in the mail body and **not**
  marked sent — those drives roll into the next cycle rather than being
  silently marked done without ever appearing in a mail the user saw.
- Content: company, role, hours remaining, an "Apply" link when
  `registration_link` is stored.
- `settings.reminder_escalation_enabled` (default `true`) gates the
  escalation batch only — the pre-existing generic deadline/event alerts are
  unaffected by this flag.

### Morning-of-event digest section

`TODAY` section at the top of the digest: OA/interview events parsed as
today's date, for drives with `my_status` past `NOT_APPLIED` (i.e. the user
actually engaged with this drive — an OA today on a drive nobody applied to
isn't "today", it's noise). Shows company, event label, time, and
`work_location` when known. No separate mail — this rides the existing
daily digest send.

### Config added

- `settings.reminder_escalation_enabled: bool` (default `true`)
- `settings.reminder_max_per_mail: int` (default `20`)
- (window hours reuse the existing `deadline_escalation_thresholds_hours`
  from the prior session — same feature, no new field needed)

---

## Verification

- Ladder: automation-source cannot downgrade or skip levels; sheet-source
  unrestricted — **PASS**, `tests/test_my_status_writeback.py::
  TestSetMyStatusLadder` (6 tests).
- Enforce mode is config-gated, default observe; UNKNOWN tier cannot write
  status even in enforce mode — **PASS**,
  `test_confirmation_integration.py::test_unknown_tier_never_writes_even_in_enforce_mode`.
- Every synthetic pattern family has a named identifier that appears in
  logs — **PASS**, `runner.py::_handle_confirmation_mail` logs
  `tier=%s family=%s`; families enumerated in `test_confirmation_detection.py`.
- Unmatched confirmations are persisted and surfaced, never dropped —
  **PASS**, `test_no_confident_match_is_persisted_and_surfaced`.
- Escalation alerts: dedup + re-arm + flagged-deadline exclusion all
  test-covered; batched into one mail — **PASS**,
  `tests/test_reminder_escalation.py` (8 tests).
- No `calendar_sync/`, no extraction-prompt, no `docs/design/01-08`
  modifications — **PASS** (confirmed by diff scope).
- Full pytest + ruff + `-m eval` green, zero regressions — **PASS**: 507
  passed / 14 deselected (up from the 440 baseline this session started
  from, across this and the prior fix-list session); ruff clean except the
  same 8 pre-existing `scripts/` errors; `-m eval` still 14 passed (score
  report untouched by this session — no live Gemini calls were spent).

## Known state, disclosed plainly

- **The working tree remains uncommitted.** Git tool calls are still denied
  by the session's permission system (same blocker as the prior session);
  the user chose to proceed with code first and handle commits separately.
  This session adds a new DB table, three new modules, and edits across six
  existing files on top of an already-uncommitted tree from the calendar-sync
  and manager-review-fixes sessions — the amount of unprotected work is now
  substantial and should be committed at the next opportunity.
- **The reference-ID matching tier is structurally inert** until a real
  sample reveals CDC's ID format and a column exists to store it (see
  Matching §1 above) — implemented per spec, not a working path today.
- **Feature 2's alert types were renamed** from last session's
  `DEADLINE_ESCALATION_48H/24H` to this session's `DEADLINE_T48/T24` — a
  live behavior change to code that fired real emails minutes before this
  session started, not a new feature from scratch.
