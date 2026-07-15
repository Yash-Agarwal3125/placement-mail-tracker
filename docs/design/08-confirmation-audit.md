# 08 — Audit: CDC Application-Confirmation Auto-Apply Feature

Read-only audit. No source or doc changes beyond this file. Every claim below
is backed by a file:line citation or a specific DB/corpus query, all
reproducible against `data/placement_mail_tracker.db` and
`scripts/eval/corpus/` as they stood on 2026-07-10.

---

## A. Are these mails already reaching the pipeline?

**A1 — DB query.** `processed_emails` has 167 rows total (2026-06-30T13:15:57Z
to 2026-07-09T17:19:22Z). A direct search for sender `noreply.cdcinfo@
vitstudent.ac.in` (exact/embedded substring, case-insensitive `LIKE
'%cdcinfo%'`) returns **zero rows**. A broader `LIKE '%CDC%'` returns 71 rows,
but every one arrived via a **Google Groups relay**, not a personal
filter-forward:

| Sender (as stored) | Count | Date range |
|---|---|---|
| `"'No Reply CDC Info' via VITIANS CDC Group..." <vitianscdc2027@vitstudent.ac.in>` | 69 | 2026-06-30 to 2026-07-09 |
| `"'No Reply CDC Info' via B.Tech...Group..." <23bai@vitstudent.ac.in>` | 1 | 2026-07-09 |
| `"'Helpdesk CDC' via VITIANS CDC Group..." <vitianscdc2027@vitstudent.ac.in>` | 1 | 2026-07-02 |
| `"'VITCC Placement' via VITIANS CDC Group..." <vitianscdc2027@vitstudent.ac.in>` | 1 | 2026-07-09 |

Gmail's Google-Groups relay rewrites the visible From to the *group* address
and wraps the original sender's display name in `"X" via Group` — the actual
`noreply.cdcinfo@vitstudent.ac.in` address (if that's even the group posts'
true originating address) never appears as the parsed sender. This is a
categorically different delivery path from the one described in this task's
role (a personal-account Gmail filter-forward that preserves the original
From header).

Sampling the 71 rows' subjects/classifications confirms they are **all mass
drive-related broadcast traffic** — new-drive announcements, OA/interview
schedule updates, shortlist/offer updates, and administrative "applied/not
applied list" notices sent to the whole batch — never an individual "you have
successfully applied to X" receipt. Classification breakdown across the 71:
`IRRELEVANT`(25), `OA_UPDATE`(18), `SHORTLIST_UPDATE`(13), `OFFER_UPDATE`(10),
`NEW_DRIVE`(4), `DRIVE_UPDATE`(1), `REMINDER`(1).

**A2 — Relevance filter / trusted senders (would a mail from this sender pass
today?).** Prediction: **yes**, via two independent paths, but this is
untested since no real mail exists to run it against.

- Classic scoring (`src/placement_mail_tracker/gmail/filters.py:33-41`):
  `PLACEMENT_SENDER_KEYWORDS["cdc"] = 30`. But `_contains_term`
  (`filters.py:303-313`) uses a word-boundary regex for terms ≤3 chars —
  `(?<![a-z0-9])cdc(?![a-z0-9])` — which does **not** match "cdc" as a
  prefix inside the contiguous word "cdcinfo" (nothing follows "cdc" that
  isn't `[a-z0-9]`... wait, "info" *is* `[a-z0-9]`, so the boundary fails and
  the match is rejected). The classic path's sender-keyword score does **not**
  fire for `noreply.cdcinfo@vitstudent.ac.in` on its own.
- Relaxed path Rule B (`filters.py:232-238`): `relaxed_sender_keywords =
  ["cdc", "placement", "career", "vitianscdc", "training"]`, matched via plain
  `in` substring check (no word boundary) against `email_lower`/`disp_lower`.
  `"cdc" in "noreply.cdcinfo@vitstudent.ac.in"` → **True**. This alone sets
  `passed_relaxed_sender = True`, and (absent newsletter/irrelevant-sender/
  negative-keyword hits in the body/subject) `is_placement` becomes True via
  `filters.py:245-250`.
- Trusted-sender auto-discovery (`src/placement_mail_tracker/utils/
  trusted_senders.py:150-168`): institutional-domain check
  (`vitstudent.ac.in` in domain) → +40; `SENDER_KEYWORDS["cdc"]` (weight 40)
  matches via the `elif keyword in local_part` substring branch
  (`trusted_senders.py:166-168`, no word boundary here either) → +40. Total
  ≈80 on the **very first sighting** ≥ `trust_threshold=50`
  (`trusted_senders.py:79`) → `is_trusted=True` immediately, feeding
  `filters.py:159-161`'s `classic_score += 55` on top.

  So in practice the sender converges to "trusted" and passes through both
  the relaxed rule and (from then on) the classic path too. **Caveat:** this
  is derived purely from reading the scoring rules; the actual subject-line
  wording of a real confirmation mail could still trip `NEWSLETTER_KEYWORDS`
  or `NEGATIVE_KEYWORDS` (`filters.py:43-56, 67-87`) and block it — untestable
  without a real sample.

**A3 — Corpus.** `grep -rli "cdcinfo" scripts/eval/corpus/*.json` → **zero
matches** across all 118 fixtures. A broader confirmation-language grep
(`"successfully applied"`, `"application.*confirm"`, etc.) surfaces exactly 2
files, both **false positives**: Unstop marketing newsletters
(`19f19326735d1c69.json` — "Register your club & win cash rewards";
`19f239b127813cf5.json` — "Sun Pharma x IIM Calcutta") from sender `Tanu Goel
<noreply@unstop.news>`, both already labeled `IRRELEVANT` in `db_context`,
whose match comes from **hidden boilerplate HTML** (`<title><!--...-->`
filler text describing a generic job-application lifecycle) — not real
content. **None of the 118 corpus fixtures are genuine CDC confirmation
mails.**

## B. What do the confirmation mails actually contain?

**Zero confirmation mails exist anywhere in the DB or corpus — reported
loudly, per the audit's own instruction.** No subject pattern, body content,
drive-ID/reference-number format, company-name format, or plain-text/HTML
determination can be reported because no sample exists. **The user must pull
real samples manually (e.g. search the monitored inbox for
`noreply.cdcinfo@vitstudent.ac.in` directly, or check the personal account's
Sent/Forwarded folder for evidence the forward ever fired) before any design
or implementation work can proceed safely.**

## C. Where would the feature attach?

**C1 — Classification.** Modeled as a plain tuple of string constants,
`EMAIL_CLASSIFICATIONS` (`src/placement_mail_tracker/extraction/
rule_engine.py:164-173`): `NEW_DRIVE, DRIVE_UPDATE, OA_UPDATE,
SHORTLIST_UPDATE, INTERVIEW_UPDATE, OFFER_UPDATE, REMINDER, IRRELEVANT`. No
`APPLICATION_CONFIRMATION` type exists. Classified by `classify_email()`
(`rule_engine.py:217-225`) via an **ordered** list of regexes
(`_CLASSIFICATION_PATTERNS`, `rule_engine.py:175-214`) — first match wins,
default `IRRELEVANT`.

Classification is computed at `scheduler/runner.py:409`, **before** the
relevance-filter call at `runner.py:411` (code order) — but the filter still
gates everything downstream: a filtered-out mail is logged with its
classification and `processed_status="skipped"` (confirmed in the DB sample
above — e.g. "Top coder online test..." classified `OA_UPDATE` but
`processed_status=skipped`) and never reaches extraction or a status write.
**Risk, unverified:** the existing `OFFER_UPDATE` pattern matches the bare
word `"congratulations"` (`rule_engine.py:192-195`) — a confirmation template
phrased "Congratulations, your application has been submitted" would misfire
as `OFFER_UPDATE` under today's ordered-pattern-list, since `OFFER_UPDATE` is
checked before any hypothetical new `APPLICATION_CONFIRMATION` pattern would
need to be inserted. Untestable without a real subject sample.

**C2 — Status writes.** `my_status` has exactly **one** write choke point:
`DatabaseManager.bulk_update_my_status()`
(`src/placement_mail_tracker/db/manager.py:534-548`), called from exactly one
site: `sheets_sync.py:229` (fed by the ALL DRIVES sheet read-back,
`sheets_sync.py:448-477` + `_my_status_to_enum`, `sheets_sync.py:899-911`).
Confirmed by reading the full opportunities `UPDATE` statement
(`db/manager.py:1049-1086`): it does **not** reference `my_status` in its
`SET` clause at all, so ordinary follow-up emails (OA_UPDATE, SHORTLIST_
UPDATE, etc.) never touch it — the column is only ever written by the sheet
read-back path today. The write itself is a blind overwrite (`my_status != ?`
only dedupes no-op writes; it does not enforce an upgrade-only ladder — a
downgrade would go through unopposed). **This is the single choke point
where an upgrade-only ladder could be enforced.**

Separately, and worth flagging because it changes the shape of "where this
feature attaches": `current_status` (a *different* axis — row lifecycle,
drives the sheet's "Current Status" column) already has a `"REGISTERED"`
detection pattern that matches almost exactly what a confirmation mail would
say — `"registration\s*(successful|confirmed|complete)|successfully\s*
registered|applied\s*successfully"` (`rule_engine.py:275-279`), wired at
`runner.py:545-548`. Critically, `REGISTERED` is **not** in the
known-thread-only gate `_ADVANCEMENT_STATUSES` (`runner.py:77`, which only
gates `SHORTLISTED/SELECTED/OFFER_RECEIVED/HR`) — so this mechanism can
already fire on a brand-new extraction, unconditionally, today, *if* a
confirmation mail reaches `rule_extract()` and matches an existing drive.
This is a distinct, already-existing capability from `my_status`, serving a
different sheet column — the design must decide whether the two should move
together or stay decoupled.

**C3 — Matching.** Company normalization:
`normalize_company_name()` (`rule_engine.py:103-130+`, suffix/noise-word
stripping + a canonical-name dict). Fuzzy drive matching:
`find_best_match()` (`src/placement_mail_tracker/utils/deduplication.py:
564-605`, rapidfuzz-based via `compare_opportunities`, first-letter-filtered
candidate list) — needs an `incoming` dict with `company_name`/`role` already
extracted. **No drive-reference-ID/registration-number extraction utility
exists anywhere in the codebase.** The closest "review" interface is
`validate_opportunity_data()` (`src/placement_mail_tracker/extraction/
validation.py:37-126`) — a free-text `validation_flags` JSON-list column
(`db/manager.py:43` JSON_FIELDS) surfaced in the sheet as "Review Flags"
(`sheets_sync.py:705`). It is generic enough to carry an "ambiguous
confirmation match" message, but it answers "does this *already-being-stored*
opp_data look implausible", not "route this unmatched confirmation
somewhere" — there is no existing interface for a confirmation mail that
matches **no** drive at all (a different failure mode than anything this
module currently handles).

**C4 — Dedup.** `processed_emails.gmail_message_id` is `UNIQUE NOT NULL`
(`db/manager.py` CREATE TABLE), enforced functionally by the
"already processed" gate at `runner.py:388-401` (skips any `gmail_message_id`
whose `processed_status` is already `processed`/`skipped`/`PERMANENT_
FAILURE`). This covers a **re-fetch of the same Gmail message**. It does
**not** cover a second, differently-ID'd message reporting the same
underlying confirmation (e.g. a resend) — there is no `sent_alerts`-style
`UNIQUE(opportunity_id, alert_type)` content-level dedup for this case today.
(Whether this actually matters depends on the design decision in C2 — an
upgrade-only ladder at `bulk_update_my_status` would make a duplicate
confirmation idempotent for free, since APPLIED→APPLIED is a no-op.)

**C5 — Eval.** `scripts/eval/labels.csv` is long-format: one row per
`(message_id, field)` pair (`message_id, received, subject, field,
rule_value, prefill_label, corrected_label, notes`). The `field` values
tracked today are exactly `company, role, deadline, oa_date, interview_date,
cgpa_cutoff, branches` (confirmed via the `MIN_PRECISION`/`MIN_RECALL` dicts
in `tests/test_eval_extraction_reliability.py:34-52`). **There is no
classification-accuracy field in the eval harness at all** — not just for
confirmations, for any of the 8 `EMAIL_CLASSIFICATIONS` values. Adding
confirmation-mail coverage would need either a new `field` value (e.g.
`"classification"`) plus corresponding scoring logic in `run_eval.py` and a
new floor entry in the test file, or folding confirmation-mail checks into
the existing `company`/`role` fields only (verifying the extractor still
attributes the confirmation to the right company, without scoring
classification itself).

## D. Delivery-chain risk

**UNKNOWN** — no forwarded CDC personal-confirmation mail exists in the DB to
measure timing against (see A1). The 71 group-relay CDC mails have
unremarkable, evenly-spread timestamps (2026-06-30 to 2026-07-09) — but they
arrive via a wholly different mechanism (Google Groups, not a personal
filter-forward) and say nothing about whether *this* forwarding path is even
active. **User-side verification items, not something this audit can
resolve:**
- Check the monitored inbox's spam/junk folder for `noreply.cdcinfo@
  vitstudent.ac.in` mail that may have landed there instead of the inbox.
- Check the personal account's Gmail Settings → Forwarding and POP/IMAP →
  confirm the forwarding address is still verified/active (Gmail
  periodically re-requires forwarding confirmation, and a lapsed
  confirmation would silently stop all forwarding with no error visible to
  the monitored account).
- Check the personal account's filter rules (Settings → Filters and Blocked
  Addresses) to confirm a filter matching `noreply.cdcinfo@vitstudent.ac.in`
  with a "Forward to" action actually exists and hasn't been edited/removed.

---

## Blockers for the feature

1. **Zero real confirmation-mail samples exist anywhere** (DB or the
   118-fixture corpus) — every downstream question (classification pattern,
   matching reliability, eval floors) is currently answered by reading rules,
   not by testing against real data. Nothing should be implemented before
   real samples are pulled.
2. **The delivery mechanism itself is unverified** — this audit cannot
   distinguish "the forward isn't firing" from "no confirmation has arrived
   since tracking began" from "it landed in spam." All three have the same
   observable signature (zero rows in the DB) but very different fixes, none
   of which are code changes.
3. **No drive-reference-ID extraction exists**, and it's unknown whether real
   confirmation mails even contain one (per blocker 1). If they don't,
   matching falls back entirely to fuzzy company/role matching, which has a
   known, already-accepted error rate in this codebase (`MIN_PRECISION
   ["company"] = 0.60` — `tests/test_eval_extraction_reliability.py:35`) —
   acceptable for a sheet row a human reviews, a materially different bar for
   an automatic status write that's harder to notice and undo.

## Design decisions the plan must make

1. Where in `_CLASSIFICATION_PATTERNS`'s ordered list would
   `APPLICATION_CONFIRMATION` go, and how does it avoid colliding with the
   existing `OFFER_UPDATE` "congratulations" pattern (`rule_engine.py:
   192-195`) checked earlier in the list? (Evidence: C1.)
2. Does the relevance filter need an explicit new rule for this sender, or is
   reliance on the relaxed-path substring match / trusted-sender
   auto-discovery convergence (both already appear to pass this sender)
   sufficient — and is "appears to pass by rule-reading" an acceptable bar
   without a real sample to confirm subject-line wording doesn't trip a
   negative keyword? (Evidence: A2.)
3. Does this feature write `my_status` only (new logic needed at
   `bulk_update_my_status`, `db/manager.py:534-548`), rely on the existing
   but currently-unrestricted `current_status="REGISTERED"` detection
   (`rule_engine.py:275-279`, `runner.py:545-548`), or both — and if both,
   should they move together or stay intentionally decoupled given they
   drive two different sheet columns? (Evidence: C2.)
4. What upgrade-only ladder ordering applies to `my_status`
   (`NOT_APPLIED → APPLIED → ...`), and should it be enforced generically at
   `bulk_update_my_status` (which would also affect the existing
   sheet-read-back path) or only in a new, separate write path used solely by
   this feature? (Evidence: C2.)
5. What happens when company/drive matching is ambiguous or fails outright
   for a confirmation mail — reuse `validation_flags`
   (`extraction/validation.py`) even though there may be no `opportunities`
   row to attach a flag to in a total-miss case, or build a dedicated review
   surface? (Evidence: C3.)
6. What's the dedup key for a confirmation mail specifically — is
   message-ID dedup (existing, covers re-fetch of the same mail) sufficient,
   or is a content-level key needed (a `sent_alerts`-style
   `UNIQUE(opportunity_id, ...)` pattern) to guard against a second,
   differently-ID'd confirmation for the same drive? (Evidence: C4.)
7. Does eval coverage require a new `labels.csv` `field` type plus new
   `run_eval.py` scoring and a new precision/recall floor, or is confirmation
   coverage folded into the existing `company`/`role` fields only, with
   classification itself left unscored (as every other classification type
   is today)? (Evidence: C5.)
