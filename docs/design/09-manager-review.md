# 09 — Manager Review: Fresh-Eyes Audit of the Shipped System

Read-only review, 2026-07-10 (mid placement season). Anti-redundancy rule
applied: docs 01–08 were re-read first; nothing below repeats them except
where labeled **[escalation]** (known issue whose priority the docs got
wrong), **[fix-regression]** (a fix introduced a new problem), or
**[claimed-fixed-but-isn't]**. Everything else is new. Every finding cites
file:line or a DB query against `data/placement_mail_tracker.db` as of today.

---

## Phase 1 — State detection

| Workstream | Docs claim | Code-verified today |
|---|---|---|
| B1 COALESCE date guard | landed (06/07) | **VERIFIED** — `db/manager.py:1060-1062`; regression tests pass |
| My Status write-back (A1/B2) | landed | **VERIFIED** — `db/manager.py:534`, called `sheets_sync.py:229`; read-back no longer swallows API errors (`sheets_sync.py:448-477`) |
| OAuth refresh wrapping ×3 (A3) | landed | **VERIFIED** — `gmail_client.py:222`, `sheets_sync.py:611`, `calendar_sync/client.py:66-71`; non-TTY fail-fast present in all three |
| Alert re-arm (B3) | landed | **VERIFIED** — `alert_generator.py:78,118` (date-suffixed keys) |
| `calendar_ok` (A5) | landed | **VERIFIED** — `status.py:45,97,143`, `health.py:142`, `main.py:124` |
| Heartbeat-on-partial (B5) | landed | **VERIFIED** — `main.py:140`, `heartbeat.py:59` |
| Extraction-session fixes (quota caps, structured output, attachments, validation layer, prompt rewrite) | landed (06) | **VERIFIED** — `settings.py:50-51`, `ai/gemini_extractor.py`, `ai/attachments.py`, `extraction/validation.py`; exercised by the passing suite |
| `calendar_sync/` | specified in 04 | **IMPLEMENTED, WIRED, TESTED, LIVE** — package exists, wired at `runner.py:801+`, 44 tests, first live run 2026-07-09 (8 events on the real calendar), reschedule + failure drills done, `CALENDAR_SYNC_ENABLED=true` in `.env` (07) |
| `-m eval` suite | opt-in gate (06/07) | **RUNS GREEN (14 passed) BUT CERTIFIES STALE DATA** — `scripts/eval/out/score_report.json` predates the current production prompt (the 06 "combined prompt never re-scored" gap is still open), so a green eval today validates the *old* prompt, not the one in production |
| pytest / ruff | green | **440 passed, 14 deselected**; ruff clean except 8 pre-existing errors confined to `scripts/` (`clean_start.py`, `run_eval.py`) |
| Git shape | — | **Git commands denied by session permissions.** From the last observed status: HEAD = `5ff4e3a` ("Qol changes"); the extraction-reliability session, the entire calendar_sync feature, docs 06–08, and all six ADR-D8 fixes are sitting **uncommitted** in the working tree. Runtime state files (`data/fetch_state.json`, `data/heartbeat.json`, `data/system_health.json`, `data/trusted_senders.json`) are git-*tracked* despite `data/*.json` being in `.gitignore` (they predate the rule), so every run dirties the tree. |

---

## Phase 2 — Findings (ordered by impact on "don't miss a date")

### F1. Time-critical follow-up mails are dropped terminally and silently at the "no identifiable company" gate — happening this week

`runner.py:526-543`: when extraction returns no company on a mail that isn't
a known-thread follow-up, the mail is logged `processed_status="skipped"` —
which is in the already-processed set (`runner.py:393`), so it is **never
retried**, stores **no error_message**, and appears in **no digest, alert, or
dead-letter count** (digest counts only `PERMANENT_FAILURE`,
`db/manager.py:769`).

DB evidence (query on `processed_emails`, 2026-07-09 rows):
- "Resmed online test is scheduled on 10th July 2026 by 1.30 pm PRP 717" —
  skipped, opp=None. **The OA was the next day.** A Resmed drive existed
  (opp 36, processed 07-06).
- "Varroc next round of selection process is scheduled on 13th July 2026 by
  08:30 am" — skipped, opp=None. Varroc drives exist.
- "Valuelabs online test, PPT and selection is scheduled on 16th and 17th
  July" — skipped, opp=None. A Valuelabs drive exists (opp 16).
- Same pattern on 07-01 for two "Top coder online test" mails.

Structural cause: CDC starts a **new thread** per announcement, so
`known_thread_followup` is False; identity then depends entirely on
extraction naming the company; and the gate fires **before** the fuzzy-match
rescue (`find_best_match` at `runner.py:563` needs `company_name` and never
runs). Doc 06 recorded the Topcoder canonical-name and Varroc-PPT
classification gaps as *extraction quality* items — **[escalation]** what 06
missed is the *handling*: extraction whiffs are inevitable, but here a whiff
on a date-bearing mail is terminal-and-invisible, during the exact week the
dates matter. The company name is literally the first word of each subject
above; a deterministic rescue (substring/normalized match of active drives'
company names against the subject before giving up) would have caught all
five. Effort: **2–3 h** including tests + a digest line for anything still
unattributed.

### F2. Event alerts are blind to the second event of any drive — the sheet sees it, the alert doesn't

`_check_event_alerts` reads **only** `next_event_date`
(`alert_generator.py:92`). That column is derived **only at
message-processing time** (`runner.py:595-597`) and picks the *earliest*
upcoming event. Once that first event passes, no code path re-derives it —
so a drive announced as "OA Jul 8, interview Jul 13" in one mail gets OA
alerts, then **zero interview alerts** (parse → `time_left < 0` → return,
`alert_generator.py:100-102`) unless a new mail happens to arrive.

Live DB shapes matching exactly this today: Varroc (oa 2026-07-08, interview
2026-07-13), Groww (oa 07-08/09, interview 07-10), Visteon (oa + interview
same day). The sheet's UPCOMING EVENTS and the digest scan
`oa_date`/`interview_date` directly (`sheets_sync.py:798-815`,
`digest_generator.py:59-66`) — only the push-alert path, the thing the user
actually relies on at T-24h, has the blind spot. Fix: make
`_check_event_alerts` iterate both date columns the way
`_build_upcoming_events` does; the date-suffixed dedup keys already prevent
double-firing. Effort: **~1 h** with tests.

### F3. The tracker spends its scarce Gemini quota on its own alert emails and GitHub CI mail

The monitored inbox receives the tracker's own output (alerts/digests to
`yashagarwal3125@gmail.com`) and GitHub notifications, and the filter passes
them: yesterday's live run (log, 2026-07-09 20:01) shows "Placement Mail
Tracker failure streak: 3" scored RELEVANT (score=40) → **Gemini called**,
and "[Yash-Agarwal3125/placement-mail-tracker] Run failed: CI - main" →
**Gemini called**. The DB also shows the tracker's own "⚠ UPCOMING EVENT:
Resmed in <23 hours" alerts being fetched and evaluated as candidates
(processed_emails rows, 06-30). With a hard 20 requests/day/model budget
(memory: quota facts) and F1 showing extraction already failing on real mail,
burning 2+ calls/day on self-noise directly increases the odds that a real
OA mail hits quota deferral. Fix: short-circuit in `_process_single_message`
(or the filter) for sender == `settings.smtp_email`, subjects with the
tracker's own alert/digest prefixes, and `notifications@github.com`. Effort:
**~1 h** with tests.

### F4. Uncommitted work spanning two major sessions

Everything since `5ff4e3a` — six ADR-D8 fixes, the extraction overhaul, the
whole calendar_sync feature (live and enabled), docs 06–08 — exists only in
the working tree. One careless `git checkout`/`clean`, disk fault, or
sync-tool mishap loses weeks of verified work; and none of it is reviewable
as units. Also: runtime state files are tracked (see Phase 1), so `git add .`
would commit heartbeat/fetch-state noise into history. Effort: **~1 h** —
commit in 3–4 logical units, `git rm --cached` the four `data/*.json` state
files so the existing ignore rule takes effect.

### F5. Quota deferral works, but is invisible where the user looks

The PENDING_EXTRACTION queue is correctly re-fetched by message ID each run
(`runner.py:264-280`) and quota deferrals deliberately don't burn the retry
budget (`runner.py:660-689`) — good composition. But the only user-visible
trace is a log warning and a generic "N email(s) failed processing" report
warning (`runner.py:823-824`). Nothing in the digest or sheet says "3 mails
are waiting on Gemini quota since 09:00". Worst realistic case: free-tier
quota resets at midnight Pacific ≈ **12:30–13:30 IST**, so a same-day-OA
mail arriving on a quota-dead morning sits unextracted (no sheet row, no
alert, no calendar event) until early afternoon with zero signal. A digest
line + ACTION REQUIRED note ("N mails pending extraction, oldest from
HH:MM") is enough. Effort: **1–2 h**.

### F6. The strict date parser rejects formats the rule engine's own regexes capture

`_DEADLINE_PATTERNS` capture times like "5.30 pm" (dot separator) and "5 pm"
(no minutes) — `rule_engine.py:330-331` allows `[:\.]?\d{0,2}` — and this
inbox routinely uses ordinals ("13th July 2026", "10th July 2026 by 1.30 pm";
see the real subjects in F1). `_STRICT_DATE_FORMATS` (`utils/time.py:42-57`)
accepts only `%I:%M %p` (colon + minutes, no ordinals). Consequence: a
perfectly good rule-extracted timed deadline gets (a) a false "only parses
under fuzzy matching and looks implausible" review flag
(`extraction/validation.py:76-82`) and (b) **no calendar event** (`derive.py`
uses the same strict parser via `parse_event_datetime`), while the sheet and
alerts (flexible parser) display it fine — three surfaces, two answers.
Fix: pre-normalize ordinals + "5.30 pm"→"5:30 pm" in `parse_datetime_strict`,
or add the missing formats. Effort: **~1 h** with tests.

### F7. Known-thread SHORTLIST/DRIVE/OFFER mails that *carry* an interview/OA date never get it extracted

This session's cost-guard carve-out forces Gemini only when
`email_classification in ("OA_UPDATE", "INTERVIEW_UPDATE")`
(`runner.py:~455-465`). But classification is first-match-wins
(`rule_engine.py:175-214`) — a known-thread mail like "Shortlisted students —
interview on 14th July" classifies SHORTLIST_UPDATE, skips Gemini, and rule
extraction **has no oa/interview date path at all** (its own comment,
`rule_engine.py:662-666`). The date in that mail is silently never captured;
the drive's status updates but `interview_date` stays stale/empty. This is
the same blind spot the carve-out fixed, one classification wider.
Options: extend the carve-out to any known-thread follow-up whose body
matches a date-near-event-keyword regex, or accept the cost and extend to all
of `_FOLLOWUP_CLASSIFICATIONS`. Needs a quota-cost decision, not just code.
Effort: **2–3 h** including deciding the trigger predicate.

### F8. Maintainability drift (stale claims a future session will trip over)

- **CLAUDE.md architecture map** omits `calendar_sync/`,
  `extraction/validation.py`, `reliability/auth_alerts.py`, and the eval
  harness — the next session's first orientation misses ~3,000 lines of
  load-bearing code. Also "Only use the main branch" + everything
  uncommitted (F4) is a bad combination. Effort: **15 min**.
- **`sheets_sync.py:3` docstring says "4 tabs"** — seven tabs are written
  (`sheets_sync.py:315` region + ACTION REQUIRED/MY APPLICATIONS/ALL
  DRIVES/UPCOMING EVENTS/Company History/RECENT UPDATES/Dashboard). Trivial
  but it's the first line a reader sees. **5 min.**
- **Eval green ≠ current prompt certified** (Phase 1): the `-m eval` badge
  is currently a false comfort; either spend the ~46 live calls to re-score
  (06's runbook) or annotate the gate. Doc-only fix: **10 min**; real fix:
  one quota-idle day.
- 01/02's file:line citations have drifted after this quarter's edits
  (e.g. B1's `manager.py:923-926` is now `:1060-1062`). Expected; noted so
  nobody "verifies" against stale line numbers.
- `_read_my_status_map` filters `!= "Not Applied"` (`sheets_sync.py:475`)
  but the sync writes the label "Not applied" (`sheets_sync.py:887`) — the
  filter never matches what the system itself writes, and user downgrades
  to "Not applied" only round-trip correctly by accident of case. Works
  today; a landmine for whoever touches either constant. **15 min** to
  reconcile.

### F9. Smaller operational notes (each real, none urgent)

- **`scripts/run_tracker.bat` appends to `logs/scheduler.log` with no
  rotation** — unbounded growth, duplicating app.log content. (app.log
  itself rotates fine: 10 MB × 5, `settings.py:83-84` — a 4-day-old incident
  *is* debuggable there, RUN_STATUS_JSON per run.) **~30 min.**
- **Inactivity detection is log-only**: after a laptop-off gap,
  `main.py:51-55` logs the warning; the user is never told "tracker was dark
  N hours, check what arrived meanwhile." One digest/alert line. **~1 h.**
- **No Gmail pagination and no at-cap warning**: `_search` issues a single
  `list()` (`gmail_client.py:299-304`) capped at `gmail_max_results=100`;
  observed intake ≈ 18.5 mails/day, so ~5 days offline brushes the cap, and
  overflow mails are silently dropped *and* skipped past by the advancing
  fetch window. A `len == max_results` warning is the cheap insurance.
  **~30 min.**
- **Digest pops calendar flags before sending** (`digest_generator.py:69` →
  `pop_pending_calendar_flags()` clears state; if the subsequent SMTP send
  fails, the flags are lost). Rare double-failure; **~30 min** to pop after
  send success. [fix-regression — introduced by this session's own wiring.]
- **`_MIN_YEAR = 2020`** (`utils/time.py:15`) admits stale years: a live row
  has `oa_date='2023-07-08'` (Flender, seen in the 07-09 dry run) that every
  parser accepts as valid. For an active-season tracker, anything before the
  current academic year is garbage. **~30 min** (tighten or flag via the
  validation layer).

---

## Phase 3 — The manager's verdict

### Top 5 actions (all season-safe)

1. **Rescue unattributed follow-up mails (F1)** — before the terminal skip,
   match active drives' normalized company names against the subject line;
   still-unmatched mails get a digest line instead of silence. *Why now:*
   it dropped three date-bearing mails this week; one was a next-day OA.
   *Effort:* 2–3 h. *If skipped:* the user must keep manually reading every
   CDC mail — which is the exact job this system exists to replace.
2. **Alert on `oa_date`/`interview_date` directly (F2)** — mirror the
   UPCOMING EVENTS loop in `_check_event_alerts`. *Why now:* three live
   drives currently have second events that will never alert. *Effort:* 1 h.
   *If skipped:* interviews following OAs get no T-48/24/4h push all season.
3. **Stop feeding the tracker its own output (F3)** — self-sender/GitHub
   short-circuit before extraction. *Why now:* every wasted Gemini call
   raises the odds a real mail defers past an OA (compounds F1/F5).
   *Effort:* 1 h. *If skipped:* chronic quota leak, worst on failure days
   (failure → alert mail → next run burns quota on it → more pressure).
4. **Commit the working tree (F4)** — 3–4 logical commits, untrack the
   `data/*.json` state files. *Why now:* weeks of live-verified work with
   zero durability. *Effort:* 1 h. *If skipped:* a single bad command erases
   the quarter.
5. **Refresh the Gemini fallback model list** — `settings.py:34-43` still
   lists two dead/retired models ("Branch A", known since 06 — this is a
   priority **[escalation]**, not a new finding). Yesterday's live log
   printed the working model inventory; pick one live fallback
   (e.g. `gemini-2.5-flash-lite`) and delete the corpses. *Effort:* 15 min +
   one test run. *If skipped:* resilience is one model deep; a 2.5-flash
   hiccup day = everything defers (F5's latency, system-wide).

Total: roughly one focused day.

### Explicitly deprioritized (real, but not now)

- **F7 (dates inside shortlist follow-ups)** — needs a quota-cost decision;
  the failure mode is a stale date, not a silent drop, and F1's rescue
  reduces its blast radius. Revisit after Top-5 lands.
- **F6 (strict-parser formats)** — annoying false review-flags, but nothing
  is lost: sheet and alerts still show the date, and the calendar anomaly is
  digest-visible. Batch it with the next extraction-quality pass.
- **F5 (quota-deferral visibility)** — the deferral itself is correct; the
  visibility polish can ride along with any digest change.
- **Gmail pagination warning (F9)** — 5× headroom at current volume.
- **Full eval re-score (~46 live calls)** — do it on the first quiet
  quota-day, not mid-week; until then treat the green eval badge as
  historical (F8).
- **From the 05 backlog, demote:** item 5 (weekly stats, M effort —
  off-season), Telegram notifier (second sender risk mid-season stands),
  prep-notes column (already parked in 05; still right). **Promote within
  05:** item 1 (deadline-escalation alerts) is now fully unblocked since My
  Status write-back landed, and pairs naturally with action #2 — it remains
  the best next feature after this list.
- **The 08 confirmation-mail feature** — its own audit found zero sample
  mails and an unverified delivery chain; do not spend season time building
  against imagined data.

### Stop-doing list

- **Stop processing self-generated and CI mail through the extraction
  pipeline** (action #3) — it is negative-value work performed 8×/day.
- **Stop keeping runtime state files git-tracked** — every run dirties the
  tree and invites accidental commits of `heartbeat.json` noise.
- **Stop treating `-m eval` green as certification** until the score report
  is regenerated against the production prompt; a passing gate over stale
  data is worse than no gate because it manufactures confidence.
- **Stop appending to `logs/scheduler.log` unrotated** — either rotate it or
  stop duplicating app.log there.

### Overall assessment

The core loop — fetch, extract, store, render, alert, and now calendar — is
genuinely solid: 440 green tests, real fail-soft behavior demonstrated live
(quota deferral, OAuth-dead drills, calendar failure isolation), and the
drive-centric model held up under a real reschedule. The system is
trustworthy for **what it manages to ingest**. Its remaining trust gap is at
the intake edges, and the DB proves it: this week alone, three date-bearing
CDC mails died silently at one gate (F1) and three tracked drives hold second
events that will never fire a push alert (F2). Until those two are fixed the
student cannot safely stop skimming CDC mail — which is the product's entire
promise. The shortest path to "rely on it": actions 1–3, about half a
focused day, no architectural risk; then commit the tree so the quarter's
work survives its author.

---

## Verification

- **Zero duplication of docs 01–08:** PASS — overlapping items appear only
  as labeled escalations ([escalation] F1-handling, fallback-model list;
  [fix-regression] calendar-flags pop) and the Phase-1 claimed-vs-verified
  table; all other findings (F2–F6, F8–F9 items) appear in no prior doc.
- **Every finding cites file:line or a traced artifact:** PASS — each
  finding names files/lines and/or specific `processed_emails` rows; F1/F2
  trace full mail→sheet/alert lifecycles (Resmed/Varroc/Valuelabs; Varroc
  OA→interview alert path).
- **Effort estimates + ≥3 real deprioritized findings:** PASS — every
  recommendation carries hours; seven items explicitly deprioritized.
- **Phase-1 table distinguishes doc-claimed vs code-verified:** PASS.
- **No architectural rewrite proposals:** PASS — every fix is a localized
  change within the existing pipeline.
