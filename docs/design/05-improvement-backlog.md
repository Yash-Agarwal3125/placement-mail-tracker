# 05 — Student-Utility Improvement Backlog

Whole-system ideas for active placement season, one paragraph each. Effort:
S ≈ hours, M ≈ a day-ish, L ≈ multi-day. "Critical path" = touches
fetch→extract→store (the part that must never break). The ADR-D8 upstream
fixes (B1 date preservation, B3 alert re-arm, etc.) are prerequisites already
committed in `03-adr-calendar-sync.md`, not backlog items — several items
below assume them.

## Recommended order (top 5)

**1. Deadline-escalation alerts for unapplied drives (T-48h / T-24h).**
Today's deadline alerts (`pmt/scheduler/alert_generator.py:50-82`) fire at
24h/4h regardless of whether the user already applied — so the inbox mixes
"you already did this" noise with genuinely missed registrations. Once My
Status is written back to `opportunities.my_status` (ADR D6, already
committed), a two-line filter makes escalation smart: eligible +
`my_status='NOT_APPLIED'` + deadline within 48h → alert; suppress deadline
alerts entirely for applied drives. Highest value-per-line in the whole
backlog: it converts the alert stream from "everything" to "things you will
actually regret missing". Value: very high. Effort: **S**. Risk: low (extends
an isolated, tested generator; dedup via existing `sent_alerts`). Critical
path: no.

**2. Morning-of-event digest email ("today: Microsoft OA 5:30 PM").**
The daily digest (`digest_generator.py`) already runs on the first cycle
after 8 AM and already scans `oa_date`/`interview_date`/`next_event_date`
within 7 days (`digest_generator.py:59-66`) — but buries today's events among
the week's. Add a "TODAY" section at the top listing events whose parsed date
is today, with times and `action_required`. During season this is the single
email worth opening every morning. Value: high. Effort: **S** (one filter +
format section in an existing, tested generator). Risk: low. Critical path:
no. Pairs with the calendar (phone shows the event; digest shows the
context: package, link, prep notes).

**3. OA/interview conflict detection.**
Two OAs on the same evening is a real, season-defining problem the system can
see before the student does. After `derive.py` exists it's nearly free: the
calendar sync already builds every event with normalized datetimes — add a
pass that finds pairs of timed events (any type ∈ {OA, INTERVIEW}) from
different companies overlapping within a window (exact overlap for timed;
same-day flag for all-day), and emit a `⚠ CONFLICT` digest line + one-shot
alert (dedup key: the pair + date, via `sent_alerts`). Value: high — this is
information no inbox view surfaces. Effort: **S** once calendar ships (M
standalone). Risk: low (pure read-side analysis). Critical path: no.
Sequencing: after the calendar module lands, since it reuses `derive_events`.

**4. `--status` CLI summary.**
A zero-API, read-only command: `python main.py --status` prints last run
status/exit code (`data/system_health.json`, `data/heartbeat.json`), fetch
window (`data/fetch_state.json`), drive counts by `current_status`, next 7
days of deadlines/events, dead-letter count, and days since each token file's
mtime (early warning for the 7-day OAuth death). Today the only way to answer
"is it still working?" is reading `logs/app.log`. The argparse scaffolding
arrives with the calendar flags (spec §4), so this is additive. Value:
medium-high (operator confidence, catches silent death early). Effort: **S**.
Risk: none (read-only). Critical path: no.

**5. Weekly stats section in the digest (applications vs. missed deadlines).**
Once `my_status` lives in the DB, a Monday digest section can compute: drives
announced last week, applied count, deadlines that passed with
`my_status='NOT_APPLIED'` (each a named regret line: "missed: Dell — FTE,
closed Fri"), plus OAs/interviews completed. The "missed" list is the
behaviour-changing part — it makes procrastination visible weekly while it's
still correctable. Reuses `updates`/`created_at` history and
`parse_datetime_flexible`; no new tables (compute on the fly for the trailing
week). Value: medium-high. Effort: **M** (date bucketing + digest section +
tests). Risk: low. Critical path: no.

## Evaluated, not in the top 5

**Telegram notifier completion.** The stub is a logger no-op
(`pmt/notifications/telegram.py:17-19`) and isn't wired into any pipeline;
settings for token/chat-id already exist (`settings.py:57-58`). Completing it
is one `urllib.request` POST to the Bot API plus routing the existing
alert/digest strings through a second channel. Real value (push beats email
for T-4h alerts), but it duplicates a working channel while items 1–3 add
*new* information — and every alert-routing change risks double-sending.
Value: medium. Effort: **S–M**. Risk: medium (a second sender multiplies
misconfiguration modes; needs its own is_configured guard + send dedup).
Critical path: no. Do after item 1 so what it pushes is already high-signal.

**Per-company prep notes surfaced in calendar descriptions.** A user-owned
"Prep Notes" column on the ALL DRIVES tab (preserved like My Status via the
same read-back map, written back via the same `bulk_update_my_status`-style
helper into a new `opportunities.prep_notes` column), injected into calendar
event descriptions by `derive.py`. Nice glue — notes appear on the phone
right when the OA starts. But it's the only item here needing a schema
column, a sheet-layout change (`ALL_DRIVES_HEADERS` is test-asserted), *and*
it makes every note edit PATCH the event (hash includes description → churny;
better to exclude notes from the hash and patch lazily). Value: medium.
Effort: **M**. Risk: medium (touches the user-owned-columns contract that
already bit us in B2). Critical path: no. Revisit after the calendar has run
quietly for a few weeks.

**Registration-link quick access in alerts.** Alert emails today show
company/role/action but not `registration_link` (`alert_generator.py:142-152`
omits it) — the one thing you need at T-4h. One row in the HTML table. Almost
too small to list, but it belongs in item 1's change set. Value: medium.
Effort: **S** (minutes). Risk: none. Critical path: no.

**Dead-letter digest deep-link.** The digest counts permanently-failed emails
but the user must hunt Gmail manually; adding the `_gmail_link`-style URL
(pattern at `sheets_sync.py:897-905`) per dead letter makes triage a tap.
Value: low-medium. Effort: **S**. Risk: none. Critical path: no.

**Extraction-quality feedback loop ("Wrong? fix it" column).** Letting sheet
edits to company/role/dates flow back into the DB (generalizing the My Status
write-back to more columns) would fix bad Gemini extractions permanently
instead of every-sync. High ceiling, but it inverts the "DB is source of
truth" rule per column and needs careful conflict rules — exactly the kind of
complexity CLAUDE.md's principles warn against during season. Value: high
*eventually*. Effort: **L**. Risk: high (write-path into the critical data
model). Critical path: yes — park until off-season.
