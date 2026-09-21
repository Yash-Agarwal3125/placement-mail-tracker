# Placement-Drive Tracker: Wrong/Missing Events — Root Cause & Fix Plan

Investigation date: 2026-09-21. All findings below come from the real Gmail
inbox, the real SQLite DB, and reading actual source lines — no guessing.

## Pipeline map

```
main.py -> scheduler/runner.py: PlacementTrackerRunner.run_once()
  _fetch_messages()                 gmail/gmail_client.py:_search / fetch_message
  gmail/filters.py:is_placement_mail()          -- relevance filter
  extraction/rule_engine.py:classify_email()    -- NEW_DRIVE / OA_UPDATE / INTERVIEW_UPDATE /
                                                    SHORTLIST_UPDATE / APPLICATION_CONFIRMATION / ...
  extraction/confirmation.py:detect_confirmation_tier() + extract_company_from_confirmation_subject()
                                                 -- for APPLICATION_CONFIRMATION mails only
  extraction/rule_engine.py:extract_from_email() -- date/venue/link/company extraction (rules first,
                                                    ai/gemini_extractor.py:GeminiPlacementExtractor as fallback)
  utils/deduplication.py                        -- fuzzy company/role match against active drives
  db/manager.py:insert_or_update_opportunity()  -- drive-centric upsert
  scheduler/runner.py:_capture_roster_verdict() -- roster attachment -> MATCHED/NOT_MATCHED/NO_ROSTER
  calendar_sync/derive.py:derive_events()       -- opportunity rows + verdicts -> desired CalendarEvent list
  calendar_sync/sync.py:CalendarSyncEngine.sync() -- diff desired vs stored, insert/patch/delete
```

## Root causes (with evidence)

### RC1 — "You're eligible" invitation mails wrongly write `my_status=APPLIED` (shared cause)

**Affects: Fareportal, TresVista, Chargebee, Malomatia.**

- `src/placement_mail_tracker/extraction/confirmation.py:247` — `_SUBJECT_COMPANY_PATTERNS`
  includes `eligible\s+for\s+(?P<company>.+?)...`, matching subjects like
  *"Congratulations! You're Eligible for Fareportal Placement Drive"*.
- `src/placement_mail_tracker/scheduler/runner.py:1036` —
  `recognized = tier == "CONFIRMED" or extracted_company is not None`. Since the
  "eligible for X" subject always yields a non-`None` `extracted_company`, `recognized`
  is `True` regardless of `tier`, and enforce-mode then calls
  `database.set_my_status(drive_id, "APPLIED", source="automation")` (runner.py:1070).
- But the mail's own body (fetched live, all four companies use the identical VIT/neo-PAT
  template) says: *"you are eligible to participate... Please log in to your placement
  portal to **confirm your participation**"* — i.e. explicitly NOT yet confirmed.
  Real body evidence (Fareportal, message `1a0a35fb35cddf33`):
  > Congratulations! Based on your profile, you are eligible to participate in the
  > upcoming placement drive with Fareportal. ... Please log in to your placement
  > portal to confirm your participation...
- The genuinely-confirming sibling mail ("Confirmed: Your Registration for X",
  message `1a0a3b05b04e2ead`) uses different, stronger wording ("this email confirms
  your **successful registration**") and is legitimately a real confirmation.
- `derive.py:has_applied = my_status not in (NOT_APPLIED, "", None)` and
  `CALENDAR_SYNC_MODE=applied_only` means this single false status write is what
  actually puts the OA/Interview events on the calendar for a drive the user was never
  shortlisted for and, in TresVista/Chargebee/Malomatia's case, never truly applied to.
- Confirmed this bug is real and specific to the "eligible for" pattern (not
  "registration for" / "date change for" / "form available") by checking all four
  companies' actual fetched mail — every one has both templates, and in every case
  it's the "eligible for" mail (not the "registration for" one) whose semantics don't
  support an APPLIED write.
- **Fix, not rewrite.** This is a scoping bug in one gate, not a flawed approach —
  split `_SUBJECT_COMPANY_PATTERNS` into two families: ones that prove the student
  acted ("registration for", "date change for", "form available for" — keep
  writing APPLIED) and the eligibility-only one ("eligible for" — still resolves/creates
  the drive so it's tracked, but must NOT write APPLIED).

### RC2 — A drive's own first announcement gets misclassified as a personal round update

**Affects: Fareportal (→ OA_UPDATE), Chargebee (→ INTERVIEW_UPDATE), Malomatia (→ SHORTLIST_UPDATE).**

- All three companies' very first "Registration - 2027 Batch" email (structured template:
  *Name of the Company / Category / **Date of Visit:** / Eligible Branches / ... / Last
  date for Registration*) states the **planned** OA+Interview schedule for the whole
  drive up front, e.g. Fareportal body: *"Online Assessment : 20-09-2026 / Interviews :
  22-09-2026"*.
- `src/placement_mail_tracker/extraction/rule_engine.py:280` `_CLASSIFICATION_PATTERNS`
  matches on bare keyword presence in subject+body[:500] — "Online Assessment"/"pre
  placement talk"/"selection process...scheduled" phrasing inside this **template
  field**, meant to describe the whole drive to every eligible/registered student, is
  indistinguishable to the regex from a genuine "you personally are now in round N"
  update. Fareportal's mail → `classify_email()` returns `OA_UPDATE`; Chargebee's →
  `INTERVIEW_UPDATE`; Malomatia's → `SHORTLIST_UPDATE`. None of the three subjects
  match any `NEW_DRIVE` pattern (line 371) — "Registration - 2027 Batch" isn't in that
  list — so the mail never gets a chance to be recognized as what it actually is.
- This independently pollutes `oa_date`/`interview_date`/`current_status` with
  unproven values, on top of RC1.
- **Fix, not rewrite** — pure regex classification genuinely cannot see "is this
  describing the whole planned process, or reporting that I personally reached this
  round" from keywords alone; but the four companies observed share one strong,
  consistent structural signature: `Name of the Company` + `Last date for
  Registration` fields in the same body. Add a structural NEW_DRIVE recognizer that
  fires on this template ahead of the keyword-based OA/INTERVIEW/SHORTLIST patterns —
  deterministic, no AI call, in keeping with CLAUDE.md's rules-first principle.

### RC3 — TresVista: unstable classification across one thread + PPT mail tagged as an OA-round roster check

- Original announcement `1a06020e0618cb11` ("...Super dream offer placement
  Registration 2027 Batch - Physical interview at vellore campus") → `IRRELEVANT`.
  None of `NEW_DRIVE`'s patterns (line 371: "campus drive/hiring/recruitment",
  "placement drive/opportunity", "registration open", "invit(ing/ation)") match this
  exact recurring VIT phrasing ("Super dream offer placement Registration").
- The real PPT-round mail `1a0c44e62a89943c` ("Re: ... Physical interview...",
  attachment `PPT Flyer_VIT Vellore_23.09.2026.pdf`) → `OA_UPDATE` (via the
  `pre[\-\s]?placement\s*talk` branch inside `_CLASSIFICATION_PATTERNS`'s `OA_UPDATE`
  entry, rule_engine.py:301). Its body is unambiguously about a **pre-placement talk**,
  not an OA. `ppt_date` extraction itself (`_extract_ppt_date`) correctly captures
  2026-09-23, so the calendar's own PPT event is right — but the roster verdict for
  this mail is recorded under `event_type='OA'` (`roster_verdicts` row:
  `opportunity_id=338, event_type='OA', verdict='NO_ROSTER'`), because
  `_ROSTER_EVENT_TYPE_BY_CLASSIFICATION`/`_resolve_roster_event_type()`
  (runner.py) maps purely off `classification == "OA_UPDATE"` → `"OA"`, with no
  awareness that *this particular* OA_UPDATE match was actually the PPT branch. So
  even a real roster attachment on a PPT mail can never produce a verdict under the
  one event_type (`PPT`) that `is_round_excluded()` actually checks for exclusion.
- No genuine per-student roster (with a name/reg-no/college-email list) ever arrived
  for TresVista in 60 days of mail — the only attachments are two JD PDFs and one PPT
  flyer. Per the "flag, don't guess" rule, TresVista's *inclusion* can't be
  automatically disproven from data available; RC1's fix (my_status shouldn't have
  become APPLIED off the "eligible for" mail in the first place) is what actually
  removes it from the calendar.
- **Fix, not rewrite** — add the recurring "Super dream offer placement
  Registration...Physical interview at <venue>" phrasing to `NEW_DRIVE`, and make the
  OA_UPDATE-via-PPT-branch case route its roster capture to `event_type="PPT"` instead
  of `"OA"` (the classification carries enough information already — just check the
  same PPT phrasing at the capture site, matching what `is_round_excluded()` was
  already extended to check on 2026-09-05).

### RC4 — Caterpillar: a same-company Hackathon drive collides with the Placement Drive

- The original Hackathon-only announcement `1a0a3cb080e65c26` ("CATERPILLAR HACKATHON
  REGISTRATION - 2027 BATCH") has NO structured "Name of the Company:" field (unlike
  the templated drives above) — unstructured prose: *"Caterpillar is planning to
  conduct a Hackathon event based hiring for **Final year students**..."*. Whatever
  extraction path ran for this mail picked up **"Final Year Students"** as
  `company_name` (opportunity id=400, `drive_kind='HACKATHON'`), not "Caterpillar".
- The real shortlist mail `1a0c2706ac3755f3` ("Hackathon 2026 - Caterpillar Next round
  of selection process is scheduled on **24th** September...", real roster attachment
  `caterpillar shortlist.xlsx`) can't fuzzy-match opp 400 by company name (stored name
  is "Final Year Students", not "Caterpillar"), so it fell through to the *other*
  active Caterpillar-named drive — opp 397, the ordinary Placement Drive
  (`drive_kind='PLACEMENT'`) — and got wrongly attached there instead. This is why
  the interview event shows on opp 397 at all, and why it's dated the 24th.
- The date-correction mail `1a0c34d6cf069ffc` ("Date Update: ... scheduled on **23rd**
  September...", same subject pattern) then can't confidently resolve to *either*
  candidate (397 now already has a same-company round stored; 400 still has the wrong
  name) → routed to `unmatched_review`, `opportunity_id=None`. The corrected date (23rd)
  never lands anywhere; the stale 24th stays.
- **Fix, not rewrite** — two independent, well-scoped fixes: (a) the Hackathon
  registration mail's company-name extraction should prefer a clean company token from
  the SUBJECT line ("CATERPILLAR HACKATHON REGISTRATION") when the body has no
  structured "Name of the Company" field, instead of falling through to a body-prose
  heuristic that grabs "Final Year Students"; (b) once (a) is fixed, re-attach the
  wrongly-parked shortlist/correction data to the right (Hackathon) opportunity via a
  one-time direct DB correction (this is historical data, not something a future code
  fix retroactively repairs).

### RC5 — Accenture: three-date OA schedule can't resolve to a single date; per-batch attachment isn't fetchable

- `1a0c389013b64cc1` ("Accenture online test is scheduled on **24th, 25th & 26th**
  September 2026 by Respective batches.") is correctly classified `OA_UPDATE` and
  correctly roster-MATCHED (100%, codename) against opportunity 304 — the pipeline
  got everything else right. But `_extract_oa_date`/the AI extractor cannot turn a
  three-day list into one `oa_date`, so it stays `None` and no OA event is ever
  created despite the confirmed match.
- The one attachment that could disambiguate per-student ("Timing slot
  accenture.xlsx") has **no `attachment_id`** in the Gmail API response for this
  message (checked directly) — it is not actually fetchable/readable by this
  pipeline at all, for reasons outside this codebase's control.
- **Per the explicit rule ("if an email is genuinely ambiguous about whether I'm
  shortlisted, flag it instead of guessing")** — extended here to "genuinely
  ambiguous about *which date* applies": the code fix detects a multi-date OA/interview
  announcement and raises a clear, visible anomaly instead of silently picking (or
  silently dropping) a date. The user has separately told me their own batch's real
  date (26th) — since no data in the mailbox can prove that programmatically, that
  one specific date is applied as a **one-time manual correction** directly to
  opportunity 304, not by teaching the general parser to "always take the last date"
  (which would silently mis-decide for a different batch/user later).

## Fix vs rewrite summary

| RC | Stage | Decision | Why |
|----|-------|----------|-----|
| RC1 | confirmation.py subject patterns + runner.py `recognized` gate | **Fix** | One over-broad OR-condition; the tier-detection design was already correct, just bypassed |
| RC2 | rule_engine.py `classify_email` | **Fix** | Add one structural NEW_DRIVE recognizer ahead of keyword patterns; regex classification stays adequate once this template is recognized |
| RC3 | rule_engine.py `NEW_DRIVE` pattern + runner.py roster event-type mapping | **Fix** | Narrow pattern gap + a mapping that ignores which OA_UPDATE sub-branch actually matched |
| RC4 | rule_engine.py company-name extraction (unstructured body) + one-time data correction | **Fix + manual data correction** | Extraction heuristic gap; historical mis-attachment needs a direct correction, not a retroactive code fix |
| RC5 | rule_engine.py date extraction (multi-date detection) + one-time data correction | **Fix (detect+flag) + manual data correction** | Genuinely unresolvable from available data per the user's own "flag, don't guess" rule |

None of these require rewriting a pipeline stage — every root cause is a scoping/
coverage gap in existing deterministic rule-based logic, consistent with CLAUDE.md's
"deterministic logic over AI calls" principle. No stage's core approach is flawed.

## Task list

- [x] Phase 1: investigate all 6 cases end-to-end with real email/DB/log evidence
- [x] Phase 2: fix/rewrite decisions (table above)
- [x] Phase 3: this plan
- [x] Phase 4: module tests using real fixtures — `tests/test_wrong_missing_events_2026_09_21.py`,
      17 tests, all passing after the fixes below (verified they fail against the
      pre-fix code by construction: each asserts the corrected output the bug
      report demanded)
  - [x] RC1: `TestRC1EligibilityIsNotApplication` (4 tests)
  - [x] RC2: `TestRC2StructuredNewDriveTemplate` (3 tests, Fareportal/Chargebee/Malomatia)
  - [x] RC3: `TestRC3TresVista` (4 tests: thread classification + PPT roster routing)
  - [x] RC4: `TestRC4CaterpillarHackathonCompanyExtraction` (1 test)
  - [x] RC5: `TestRC5AccentureMultiDateFlag` (2 tests)
  - [x] Regression: `TestRegressionRealCorrectEmails` (3 tests: Caterpillar's real
        MATCHED shortlist mail, a real registration-confirmation mail, a real
        single-date OA_UPDATE mail — unaffected by any of the above)
- [x] Phase 5, iteration 1: implemented RC1-RC5 code fixes (confirmation.py,
      rule_engine.py, runner.py), full suite green (733 passed), ruff clean.
      Also found and fixed one more gap while designing the correction list
      below: a NEW_DRIVE template's own "Date of Visit: PPT & Test: ..."
      field (real Chargebee mail) fabricates `ppt_date` the same way it
      fabricates oa/interview dates — now suppressed too, unless the mail's
      own distinguishing signal is a real PPT invitation (`is_ppt_mail`).
- [ ] One-time direct data corrections — see the exact list below. **Not yet
      applied — awaiting user confirmation per the rules.**
- [ ] Verifier pass: dry-run the fixed pipeline against the 6 real cases +
      broader recent mail, compare against acceptance tests

## Proposed one-time data corrections (NOT YET APPLIED)

The code fixes above stop these mistakes for *future* mail; they don't
retroactively repair opportunity rows already written by the bugs. Each
row below was checked against the real mail history for a genuine
confirming/round-specific mail before deciding what to keep vs. clear.

| # | Opportunity | Field | Current (wrong) | Corrected | Why |
|---|---|---|---|---|---|
| 1 | 394 Fareportal | oa_date | 2026-09-20 | *(cleared)* | Sourced from the misclassified registration mail's own schedule preview, not a personal update (RC2) |
| 2 | 394 Fareportal | interview_date | 2026-09-22 | *(cleared)* | Same as above |
| 3 | 394 Fareportal | my_status | APPLIED | **APPLIED (unchanged)** | Real "Confirmed: Your Registration" mail exists — this one's genuine |
| 4 | 338 TresVista | interview_date | 2026-09-25 | *(cleared)* | Sourced from the original announcement's own "Date of Visit" field, never a personal update |
| 5 | 338 TresVista | ppt_date | 2026-09-23 | **2026-09-23 (unchanged)** | Real, separate PPT-invitation mail exists — PPT is shown to all applied students by existing design, see the flag below |
| 6 | 338 TresVista | my_status | APPLIED | **APPLIED (unchanged)** | Real "Confirmed: Your Registration" mail exists |
| 7 | 422 Chargebee | oa_date | 2026-09-26 | *(cleared)* | Same schedule-preview issue as Fareportal |
| 8 | 422 Chargebee | interview_date | 2026-09-28 | *(cleared)* | Same |
| 9 | 422 Chargebee | ppt_date | *(none stored)* | *(stays none)* | Same schedule-preview field, not a real PPT invite (`is_ppt_mail` is False for it) |
| 10 | 422 Chargebee | my_status | APPLIED | **APPLIED (unchanged)** | Real "Confirmed: Your Registration" mail exists |
| 11 | 410 Malomatia | oa_date | 2026-10-13 | *(cleared)* | Same schedule-preview issue |
| 12 | 410 Malomatia | interview_date | 2026-10-15 | *(cleared)* | Same |
| 13 | 410 Malomatia | ppt_date | 2026-10-01 | *(cleared)* | Same schedule-preview field |
| 14 | 410 Malomatia | my_status | APPLIED | **NOT_APPLIED** | No genuine confirmation mail ever arrived for Malomatia — only the two "eligible for" invitations (RC1) |
| 15 | 400 Caterpillar Hackathon | company_name | "Final Year Students" | "Caterpillar" | RC4 extraction fix, applied retroactively to the historical row |
| 16 | 400 Caterpillar Hackathon | interview_date / current_status / roster verdict | *(none — never received them)* | Interview_date=2026-09-23, current_status=OA/INTERVIEW, roster verdict MATCHED (moved from opp 397) | Re-attach the real shortlist data (RC4) to the drive it actually belongs to |
| 17 | 397 Caterpillar Placement Drive | interview_date / current_status | 2026-09-24 / OA-ish | *(reverted to this drive's own real state, pre-collision)* | This data belonged to the Hackathon drive, not this one (RC4) |
| 18 | 304 Accenture (Training) | oa_date | *(none — unparseable multi-date)* | 2026-09-26 | Roster-MATCHED (real), but the mail names 3 dates with no readable per-batch attachment (RC5) — applying **your own stated fact** (26th) as a manual, one-time correction, not a general "always pick the last date" rule |

## Flag for you (per the "don't guess, ask" rule)

**TresVista's PPT event (#5 above)**: your original report said "no event" for
TresVista overall. Digging in, TresVista's *interview* date was fabricated
(cleared, #4) — but its *PPT* invitation is real (a dedicated mail, "Dear
Students... invite you to the preplacement talk... Wednesday, September 23")
and PPT events are shown to every applied student by an explicit design
decision you made earlier this project (2026-09, "a pre-placement talk is
open to every applied student, not a shortlisted subset"). Do you want the
TresVista PPT event kept (matches that earlier rule), or removed too (matches
"TresVista should have no event")? Everything else in this table proceeds
either way — only this one line depends on your answer.

## Resulting calendar event changes (once corrections above are applied)

- **Removed**: Fareportal OA + Interview; TresVista Interview (PPT — see flag
  above); Chargebee OA + Interview; Malomatia OA + Interview + PPT
- **Corrected date**: Caterpillar's Interview moves from the 24th to the 23rd,
  and moves from the wrong opportunity (397) to the right one (400)
- **Added**: Accenture OA event on the 26th (opportunity 304), currently
  missing entirely
- Some of the "removed" events are already frozen (`done`/`cancelled`
  status) from earlier syncs and will need a direct one-time Calendar API
  delete, same as three earlier sessions' cleanups (the diff loop never
  revisits frozen rows) — not a new bug, the known blind spot.

## Acceptance tests

- No event for Fareportal, TresVista Financial, Chargebee, or Malomatia
- Caterpillar event on Sep 23 (not 24) with the correct time/venue/link
- Accenture event on Sep 26 with the correct time/venue/link
- All currently-correct events (Tresvista... wait, superseded above) — i.e. the
  regression fixtures' expected outputs — remain unchanged

## Iteration log

- **Iteration 1** (2026-09-21): Phase 1-3 complete (this document). Starting Phase 4.
