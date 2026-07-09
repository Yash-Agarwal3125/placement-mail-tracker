# 06 — Extraction Reliability: Fixes, Findings, and Runbook

Status: **Implemented** (this session). Grounds every claim in the eval harness
under `scripts/eval/` (118-mail real corpus, human-verified `labels.csv`) or in
source file:line citations — no claim here is inferred from output patterns
alone without checking the code that produced them.

## Config-branch note (read this before trusting any number below)

The production Gemini fallback chain is **Branch A: untouched**. As of this
session, `pmt/config/settings.py:33-40` still configures:

```
gemini_model = "gemini-2.5-flash"
gemini_fallback_models = [
    "gemini-2.5-flash-lite-preview-06-17",  # retired
    "gemini-2.0-flash",                      # 0 free-tier quota on this key
    "gemini-2.0-flash-lite",                 # 0 free-tier quota
    "gemini-1.5-flash",                      # no longer exists on this key
    "gemini-1.5-flash-8b",                   # no longer exists on this key
]
```

`data/ai_model_health.json` confirms 0 successes across all attempts for the
four dead fallbacks. This was measured, not patched, this session — the model
list itself is an intentionally separate decision the user has not made yet
(swapping in living models is a config change, not a code fix, and changes
what "the fallback chain" even means for future eval baselines).

## The abandoned T0–T8 taxonomy

The initial plan for this session assumed a T0–T8 failure taxonomy already
existed from a prior investigation. It didn't — it wasn't recoverable from
this repo, its git history, or this session's context. A reconstruction was
attempted from fragments (an eval-script docstring, eval-gate hints in the
task brief) and abandoned once real data came in: **the majority of the
"misses" the taxonomy was meant to classify weren't extraction defects at
all** — they were an eval-harness scoring bug, a genuine but narrow
production bug, and deliberate system behavior the ground-truth labels
disagreed with. Forcing 39 real misses into T-codes nobody could fully define
would have been a weaker deliverable than root-causing each one. What follows
is the real breakdown instead.

## Corpus

118 real, PII-redacted mail fixtures (`scripts/eval/corpus/*.json`), replayed
through the actual production path (`PlacementTrackerRunner._process_single_message`)
against a throwaway in-memory SQLite DB — filter gating, rule/Gemini
arbitration, dedup, and the update path all behave exactly as production.
Ground truth: `scripts/eval/labels.csv`, 423 rows (message × field), human-
verified. 61 of 118 mails are placement-relevant; ~47 reach the extraction
stage per replay.

## Fixes landed this session, eval- or unit-test-confirmed

| # | Bug | Location | Confirmed effect |
|---|---|---|---|
| B1 | Follow-up mail with no date silently NULLs a stored `deadline`/`oa_date`/`interview_date` | `db/manager.py:924-926` (COALESCE guard) | Regression test: date set → follow-up omits it → date persists. Landed first, blocking, before any eval score was trusted. |
| — | Eval scorer's own `_norm()` mis-normalized a JSON-serialized DB string for the `branches` field (string-vs-list mismatch), inflating "branches wrong" | `scripts/eval/run_eval.py:375-393` | branches score 17/34 correct → **17/17 (100%)** once the scorer was fixed — most of the apparent defect was the scorer, not extraction. |
| — | `branches_allowed` (and by the same mechanism `hiring_process`/`important_notes`) got double-JSON-encoded on every follow-up update | `db/manager.py:1217-1225` (`_normalize_list` not idempotent — `insert_or_update_opportunity` normalizes once, then passes the normalized dict into `update_opportunity`, which normalizes *again*) | Confirmed root cause via the `_normalize_list('["CSE",...]')` round-trip; branches went from 17/34 → **17/17 (100%)** — this was the one *real* bug behind the branches score, on top of the scorer bug above. |
| — | `_LABEL_PREFIX` regex missing "Update:" (no trailing "d") and "Join immediately:" | `extraction/rule_engine.py:46-50` | Fixed 2 mails' company mis-extraction (prefix leaking into the company name). |
| — | `needs_gemini` never checks whether oa_date/interview_date were found at all — only company/role/status | `extraction/rule_engine.py:648-661` | Now returns True for OA_UPDATE/INTERVIEW_UPDATE regardless of company/role confidence — these two fields have **no rule-based extraction path at all** (no field even exists on `RuleExtractionResult`), so confidence in the rest of the extraction was never a valid reason to skip Gemini for them. |
| — | The Phase-6 cost guard (`known_thread_followup`) blanket-skipped Gemini for *any* known-thread follow-up, including OA_UPDATE/INTERVIEW_UPDATE | `scheduler/runner.py:426-443` | **User-approved tradeoff** (increases Gemini calls specifically for these two classifications on already-tracked threads). The existing cost-guard test (`test_known_thread_followup_skips_gemini`) was updated to a non-date classification rather than silently broken; a new test proves the OA/INTERVIEW carve-out fires. |
| Quota | Retry chain: 3 attempts × 6 models = up to 18 live calls/email | `ai/gemini_extractor.py` (`extract_from_text`), `config/settings.py:44-51` | Capped to `gemini_max_models_to_try` (2) × `gemini_max_retries` (1) = 2 calls/email ceiling. |
| Quota | ~11% JSON-parse failure rate on live responses (doubled braces, truncation) | `ai/gemini_extractor.py` (`_generate_content`, `_extract_result_from_response`) | Migrated to `response_schema=PlacementExtraction` (structured output) with the manual JSON-repair path kept as a fallback. Not eval-visible yet — see "Known gaps" below. |
| Quota | Quota-exhausted mails silently degraded to rule-only and got marked "processed" (data lost until the mail ages out of Gmail's default retention) | `ai/gemini_extractor.py` (`GeminiQuotaExhaustedError`), `scheduler/runner.py` (`PENDING_EXTRACTION` deferral) | New typed error propagates instead of falling back; the mail is marked `PENDING_EXTRACTION` (an existing, already-wired retry-queue status — not invented this session) and retried on a later run instead of lost. Deliberately does **not** count toward `_RETRY_MAX`/dead-letter (quota resets on its own schedule, isn't a defect in the email). |
| Input | No mail Date header reached the extraction prompt, so relative dates ("this Friday") had no anchor | `ai/gemini_extractor.py` (`clean_email_content` gains a `Received:` line + a prompt rule) | Checked before spending anything: **zero** genuine placement mails in this corpus use relative dates (all state absolute dates) — the fix is real and tested, but this specific corpus can't demonstrate its value. |
| Input | Attachments (PDF/xlsx/images) were never fetched — `extract_body_text` only walks text/plain and text/html MIME parts | `gmail/gmail_client.py` (`extract_attachment_parts`, `fetch_attachment_bytes`), new `ai/attachments.py` | `.xlsx` parsed with **stdlib only** (`zipfile` + `ElementTree` over the OOXML shared-strings/sheet XML) — no dependency added. `.pdf` needs a dependency (`pypdf>=6.6.2`, added to `requirements.txt`) since stdlib has no PDF text support. Images routed to Gemini multimodal (`Part.from_bytes`), only inside an already-happening Gemini call — never a new call. Cost measured directly (not guessed): 4/118 corpus mails are relevant + need Gemini + carry an image; added tokens are ~500–1500/mail, riding the existing call. |
| Prompt | No explicit null-discipline, DMY convention, or field-definition rules; no few-shot examples | `ai/gemini_extractor.py` (`SYSTEM_PROMPT`, `build_extraction_prompt`, new `FEW_SHOT_EXAMPLES`) | 4 few-shot examples sourced from real, PII-checked corpus mails, cross-verified against `labels.csv`. Not eval-visible yet — see "Known gaps." |
| Validation | No boundary check for implausible extracted values (past-dated events, deadline-after-interview, CGPA out of [0,10], low-confidence extractions) | New `extraction/validation.py`; new `PlacementExtraction.confidence` field; new `validation_flags` DB column (`db/manager.py`), surfaced as "Review Flags" in the sheet (`sheets/sheets_sync.py`) | Never blocks storage (fail-soft). A subtle gap — a follow-up that doesn't restate a flagged date would otherwise silently un-flag the drive — was caught and fixed: validation runs against the *effective* (DB-merged) dates, not just what the current mail restated. |
| A1/B2 | My Status was sheet-only; the DB column always read `NOT_APPLIED`. A transient sheet-read failure wiped every user-set status. | `db/manager.py` (`bulk_update_my_status`), `sheets/sheets_sync.py` (`_my_status_to_enum`, write-back call, `_read_my_status_map` no longer swallows API exceptions) | The read-back now persists into the DB every sync. A genuine API failure now propagates into the existing 3-attempt retry wrapper (`sync_active_opportunities`) instead of being silently treated as "sheet is empty" — a failed sync now leaves the sheet (and its My Status values) untouched rather than corrupting them with blanks. |
| A3 | `credentials.refresh()` unwrapped in both OAuth stacks; a dead refresh token would hang up to 120s trying an interactive consent flow on a headless scheduled run | `gmail/gmail_client.py`, `sheets/sheets_sync.py` (`authenticate()`), new `reliability/auth_alerts.py` | `RefreshError` now raises a typed, clearly-worded auth error ("OAuth dead — re-consent needed for Gmail/Sheets"); `run_local_server` is never launched when `sys.stdin` isn't a TTY. A one-shot SMTP alert fires immediately (deduped until the token is re-consented) instead of waiting for a vague multi-run failure-streak email. |
| B3 | Alert dedup (`sent_alerts`, `UNIQUE(opportunity_id, alert_type)`) never re-armed after a reschedule — a moved OA/deadline would get **no** new alert | `scheduler/alert_generator.py` | `alert_type` is now date-suffixed (e.g. `EVENT_24H:2026-06-17`); no schema change needed. |
| A5 | `RunReport` has no way to represent a calendar-sync failure (mechanical prerequisite for the not-yet-built calendar module) | `reliability/status.py`, `main.py`, `reliability/health.py` | `calendar_ok: bool = True` added symmetrically everywhere the other four `*_ok` fields are threaded. **Currently inert** — nothing sets it False yet, since `calendar_sync/` doesn't exist. Scaffolding-ahead-of-use, flagged here rather than hidden; revisit when the calendar module lands. |
| B5 | Heartbeat only advanced on full `SUCCESS`; a single chronic warning (`PARTIAL_SUCCESS`) starved it, producing misleading "Tracker inactive for N hours" alerts | `reliability/heartbeat.py`, `main.py` | Heartbeat now writes on `SUCCESS` **and** `PARTIAL_SUCCESS`, recording the actual status; only `FAILED` withholds it. |

## Confirmed NOT bugs (deliberate, already-tested behavior — left untouched)

- **Company legal-suffix stripping** (`Ltd`/`Pvt`/`LLP`/`Private Limited`/`India`/etc.
  removed) — `extraction/rule_engine.py:22-27` (`_STRIP_SUFFIXES`), covered by
  `test_dell_variants`/`test_tata_motors_variants`/`test_noise_stripping`. This
  is the dedup/normalization mechanism CLAUDE.md's drive-centric model depends
  on (merging "Varroc Engineering" and "Varroc Engineering Ltd" as one company
  across mails). ~13 of the raw "company wrong" misses are this working
  exactly as designed — `labels.csv`'s ground truth uses the raw legal name,
  which isn't what the system is supposed to store.
- **Role showing as "Super Dream Internship"/"Dream Internship"** for VIT's
  selection-list email format — `extraction/rule_engine.py:370-381`
  (`_ROLE_PATTERNS`), an inline comment confirms this regex is purpose-built
  for exactly this subject format, not an accident. ~6 misses are this
  behavior; whether the tier name is an acceptable stand-in "role" for these
  congratulatory mails is a product judgment call, not a code defect — left
  to the user to decide if it should change.
- **~8 misses are drives whose value was legitimately superseded by a later,
  correct follow-up mail** (COALESCE/thread-merge working as intended).
  `labels.csv` labels are per-mail; the eval scorer compares each label
  against the *final* drive state, which is unfair whenever a later mail
  genuinely and correctly updates the value — not a defect in either the
  extraction or the storage layer.

## Known gaps — not closed this session

- **The combined prompt (Date-header anchor + null-discipline/DMY/few-shot
  rewrite) has never been eval-scored end-to-end.** Both changes altered the
  prompt text, which invalidates the eval cache (`sha256(model+prompt)` keyed)
  — a full re-score needs ~61 live Gemini calls against a 20-request/day/model
  free-tier limit. A 15-call partial attempt was made and aborted cleanly
  (the harness deliberately writes no output unless the full corpus
  completes, by design — a partial replay would misrepresent production
  behavior); those 15 responses are now cached, reducing a future complete
  run's need to ~46 calls. **Regression coverage for this session's prompt/
  validation/quota/attachment changes is unit-test only** (the new tests
  listed in each fix's file) — not eval-eval-confirmed. Run the harness fully
  once quota allows, before trusting eval numbers for anything the last two
  subagents' prompt changes touch.
- **Structured output (`response_schema=PlacementExtraction`) is not yet
  visible in eval numbers** for the same cache-invalidation reason as above —
  the cache stores raw `.text`; a cached `SimpleNamespace(text=...)` has no
  `.parsed`, so every replay falls through to the legacy manual-parse path by
  construction. The improvement is real in production, structurally invisible
  in eval until fresh cache entries exist.
- **`Topcoder`/`Top Coder` missing a canonical name-merge entry** — surfaced
  only because Gemini now runs 4 more times (the cost-guard carve-out),
  revealing a pre-existing, unrelated gap in `_CANONICAL_NAMES`. One-line fix
  when picked up; not fixed this session (out of the approved scope for this
  pass).
- **"Pre-Placement Talk...scheduled on `<date>`" doesn't match any
  OA_UPDATE/INTERVIEW_UPDATE classification keyword**, so 2 Varroc corpus
  mails' oa_date/interview_date still fall through the cost guard even after
  the carve-out (their classification is something else entirely, not one of
  the two carved out). Needs either a classification-pattern expansion or
  broadening the carve-out to all `_FOLLOWUP_CLASSIFICATIONS` — a bigger cost
  tradeoff than what was approved this session.
- **Two single-instance (n=1) cases** — a spurious `interview_date` on a
  "2 New Drives" mail (possibly cross-contamination between two drives
  announced in one email) and a lost `deadline` on a "Re: Top coder" reply
  (the rule engine found nothing, but `needs_gemini` didn't trigger despite
  new information being present) — both real but too low-volume and
  insufficiently understood to fix confidently this session.
- **CloudSEK/Valuelabs multi-round OA dates colliding**: a drive can have more
  than one real OA/interview round (a reschedule, or a genuinely separate
  second round), but the schema has exactly one `oa_date` column per drive —
  the later round's value always wins, silently discarding the earlier one.
  This is a schema-shape limitation, not an extraction bug; worth a design
  note for anyone building the calendar module, since the same drive→event
  cardinality assumption shows up there too.

## Runbook: closing the gaps above

1. **Re-score the combined prompt** once today's Gemini quota resets:
   `python scripts/eval/run_eval.py --extract --gemini-all --max-live-calls 20`
   (repeat across days as needed — the cache persists, so each day's calls
   are never wasted), then `python scripts/eval/run_eval.py --score`. Compare
   against the last table in this doc; update `tests/test_eval_extraction_reliability.py`'s
   floors only if the new numbers are a genuine, understood improvement.
2. Run the opt-in eval pytest marker any time after regenerating
   `score_report.json`: `python -m pytest -m eval`. It's skipped by default
   (`pyproject.toml`: `addopts = "-m 'not eval'"`) since it depends on
   artifacts under `scripts/eval/` (git-ignored, contain real mail content)
   that don't exist in a fresh checkout.
3. If a field's floor in `tests/test_eval_extraction_reliability.py` ever
   fails after a change that should be neutral or positive, that's a real
   signal — investigate before adjusting the floor down.
4. `scripts/eval/` remains temporary instrumentation, not production code
   (per its own README) — it is not part of the shipped package and should
   stay excluded from normal `pytest`/`ruff` gates the way it already is.

## Verification checklist (per the session's own scope contract)

- [x] B1 landed first, with a passing regression test, before any eval score
  was trusted.
- [x] Config branch (A) explicitly reported above, reflected honestly.
- [x] No forced T0–T8 classification — the real, root-caused breakdown is
  used instead, with the reasoning for abandoning the taxonomy stated plainly.
- [x] Every implemented fix maps to a verified root cause (file:line) with a
  test; nothing was "fixed" by pattern-matching output alone.
- [ ] Full before/after per-field table showing zero regressions — **partial**:
  confirmed for the two direct DB/rule fixes (branches double-encode,
  `_LABEL_PREFIX`, `needs_gemini`/cost-guard); **not yet confirmed** for the
  combined final prompt, per "Known gaps" above.
- [x] Full `python -m pytest` green (396 passed, 14 opt-in eval tests
  deselected by default) throughout every fix in this session.
- [x] No `calendar_sync/` module code was written.
- [x] No personal data of other students appears in any new file (few-shot
  examples and eval fixtures were checked, not assumed, before use).
- [x] Every subagent's changes were reviewed against the actual diff (not
  just their self-report) and integrated serially, one workstream at a time,
  since worktree isolation was declined for this run.
