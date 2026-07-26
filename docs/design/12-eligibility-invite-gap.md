# 12 — Eligibility-Invite Gap: Investigation, Reference-ID Fix, Phase 3 Recommendation

Triggered by a real mail: "Ion Group" eligibility invite (sender
`noreply.cdcinfo@vitstudent.ac.in`, message `19f60b9495b5b9c9`, received
2026-07-14). Correctly classified `APPLICATION_CONFIRMATION`/tier `UNKNOWN`
(it genuinely isn't a confirmation) and correctly found no matching drive,
landing in `unmatched_confirmations` — not a bug in the classification/match
logic itself. This doc covers what *was* found: a real reference-ID
extraction bug (fixed), and the case for/against a new capability (not
built — checkpointed here).

## Phase 1 — Investigation (read-only)

### Did a broadcast announcement for Ion Group ever arrive?

Searched `processed_emails.subject` (all mail, all senders, all statuses) for
any variant of "Ion"/"ION"/"Ion Group". Exactly one row matches — the NeoPAT
eligibility invite itself (`gmail_message_id=19f60b9495b5b9c9`,
`sender=noreply.cdcinfo@vitstudent.ac.in`). No mail from the broadcast
address (`vitianscdc2027@vitstudent.ac.in`) or any other sender mentions Ion
Group in the subject.

**Confidence: MEDIUM.** This is a subject-line search only — `processed_emails`
does not store the body for ordinary (non-confirmation-sender) mail, so a
broadcast that mentioned "Ion Group" only in the body, with an unrelated
subject line, would not surface here. Given every broadcast sample observed
in this corpus puts the company name in the subject (see the ~50 `vitianscdc2027@`
rows sampled below — "CloudSEK...", "Decode Age...", "Green Tiger Mobility...",
etc., all company-name-first subjects), this is a reasonable but not airtight
inference. **Verdict: no broadcast has arrived for Ion Group as of this
session** — this is a data-availability gap (the announcement hasn't happened
yet, or arrived through a path this system doesn't see), not a pipeline bug.

### Gap quantification

`unmatched_confirmations` currently holds exactly **2** rows total:

| id | message | Genuine "no drive anywhere" gap? |
|---|---|---|
| 1 | TCS NQT confirmation (`19f508fea985dbe7`) | **No** — `opportunities` row 50 (`TCS_2026_UNKNOWN_ROLE`) exists and would now fuzzy-match confidently under the current (post-Fix-2) matching code. This row is stale: `processed_emails.opportunity_id` is still empty and `opportunities.my_status` for TCS is still `NOT_APPLIED` — the 2026-07-13 fix was validated in isolation (per `docs/design/10-confirmation-and-reminders.md` §"First real sample") but the live mail was never reprocessed against it, because `processed_status='processed'` makes the retry guard treat it as already handled. This is exactly the gap `scripts/backfill_confirmations.py` exists to close — running it would resolve this row (subject to `CONFIRMATION_MODE=enforce`, still `observe` today, correctly not flipped by this session). |
| 2 | Ion Group invite (`19f60b9495b5b9c9`) | **Yes** — no `opportunities` row for Ion Group exists by any path (confirmed above). |

**So the actual gap size, of what this session was asked to quantify, is n=1** — a
single real occurrence in the entire corpus (60 active drives, 2 real
confirmation-sender mails captured to date). This is a very small sample to
generalize a design decision from.

### Content richness: invite vs. broadcast

Sampled the Ion Group invite against two broadcast-sourced drives already in
`opportunities`:

| Field | Ion Group invite (`19f60b9495b5b9c9.json`) | Cisco (broadcast, `CISCO_2026_SOFTWARE_ENGINEER_INTERN_INTERN`) | Infosys (broadcast, `INFOSYS_2026_SPECIALIST_PROGRAMMER_(TRAINEE),_FULLTI`) |
|---|---|---|---|
| Company | Yes ("ION Group") | Yes | Yes |
| Role | **No** | Yes ("Software Engineer Intern") | Yes |
| Deadline | **No** | Yes (`2026-07-17T12:00`) | Yes (`2026-07-12`) |
| Package/stipend | **No** | Yes (₹98,000–1,21,300/month) | — |
| Work location | **No** | Yes (Bangalore) | — |
| CGPA/eligibility criteria | **No** | — | — |
| Drive/reference number | Yes (`pat-PL-2026-1101`, only extractable after the Phase 2 fix) | — | — |

The other captured confirmation-sender sample (TCS NQT,
`19f508fea985dbe7.json`) is equally thin — it's a genuine application
confirmation, not an announcement, so it never carried deadline/role/package
either. This is a structural property of the sender, not a one-off: NeoPAT
mail (both confirmations and invites) is built to confirm/invite, not to
announce full drive details. **A drive row sourced only from an invite mail
would have company name + (now) a reference ID, and nothing else** — no
deadline, no role, no eligibility criteria, no package, no location. Every
downstream feature that depends on those fields (deadline escalation,
morning-of-event digest, action-required countdown, eligibility filtering)
would see a structurally incomplete row until a broadcast (if one ever
arrives) fills it in.

## Phase 2 — Reference-ID extraction fix (implemented)

**Bug** (demonstrated on the real Ion Group mail): `extract_reference_id`
(`extraction/confirmation.py`, previously lines 95-99) had every part of its
label optional — the `id`/`no`/`number` qualifier and the `:`/`-`/`#`
separator both — so a bare occurrence of the word "drive" anywhere in
ordinary prose was itself treated as a label, with only whitespace
(including newlines) required before the "captured" value. On the real mail
this matched "Drive" at the very end of the subject
("...ION Group Placement **Drive**") and captured "Placement" from the body's
own opening line ("**Placement** Drive Invitation..."), nowhere near the real
"Drive Number:\npat-PL-2026-1101" field twelve lines later. Traced directly:
```
full match span: 'Drive\nPlacement'
captured group:  'Placement'
```

**Consequence if left unfixed:** harmless in this specific case only because
no active drive's `drive_id` happens to equal "Placement" — but the
reference-ID match path (`extraction/confirmation.py`, `find_confident_drive_match`)
is an exact-string match with **no threshold and no uniqueness-margin
check**, unlike fuzzy company matching. A future mail producing the same
kind of garbage extraction that happened to collide with a real `drive_id`
slug would cause a wrong, fully-confident, automatic status write in enforce
mode.

**Fix:** the separator (`:`/`-`/`#`) is now mandatory and must appear on the
same line as the label. Bare "drive" is no longer a label at all — only
"Drive Number"/"Drive ID"/"Drive No" (i.e. "drive" *with* a qualifier) counts,
matching the labels this feature was actually built to recognize
(`docs/design/10-confirmation-and-reminders.md`'s own examples: "Drive
Number:", "Reference:", "Registration ID:"). "Reference"/"Registration"
remain valid bare (with mandatory colon), since real mail may write either.
The value may still be on the next non-blank line, matching the real CDC
mail's "Label:\nvalue" layout.

Verified:
- `test_real_ion_group_sample_extracts_labeled_id_not_a_bare_word` — the
  exact real mail text now correctly returns `pat-PL-2026-1101`.
- `test_bare_drive_word_in_prose_is_not_treated_as_a_label` — a synthetic
  mail repeating "drive" in prose with no real ID field returns `None`.
- All 24 pre-existing tests in `tests/test_confirmation_detection.py` still
  pass unchanged (26 total now).
- Full suite: 520 passed, 0 failed; `ruff check` clean on both touched files.

## Phase 3 — New capability: NeoPAT-invite-sourced drive creation

**Design question:** given the Phase 1 data, is it worth treating NeoPAT
eligibility-invite mails as a fallback drive-creation source when no
`opportunities` row exists for the mentioned company?

### Case for

- It would have given Ion Group *some* row instead of none, closing the
  visibility gap for a student who might otherwise miss the drive entirely
  if no broadcast ever follows.
- The reference-ID fix (Phase 2) makes the drive number extractable now,
  which is new information this pipeline didn't have before — a `DRAFT`
  row could at least carry company + drive number as a stable key for a
  later broadcast to merge into.

### Case against

- **The gap this would address occurred exactly once**, out of 60 active
  drives and 2 confirmation-sender samples captured to date. Building a new
  drive-creation path, a new merge-on-broadcast-arrival mechanism, and a new
  DRAFT/low-confidence state — all to close a gap this thin — is a
  meaningfully larger addition than the problem it solves has demonstrated
  it needs, going by what's actually been observed rather than what might
  happen.
- **The resulting row would be structurally incomplete** (Phase 1 content
  comparison above): no deadline, no role, no eligibility criteria, no
  package, no location. Every alerting/digest feature that depends on those
  fields would either silently skip this row (deadline escalation requires
  a deadline; morning-of-event requires an event date) or need new
  special-casing to handle "drive exists but has almost nothing in it" —
  which is a second layer of new complexity beyond the creation path itself.
- **A cheaper, already-partially-solved alternative exists**: the mail is
  already correctly captured and already correctly surfaced in
  `unmatched_confirmations` for manual review — which is visible today only
  via direct DB query, not (per the review-flags-readiness finding from the
  prior session) via any digest or sheet surface. The higher-value, much
  smaller fix is making `unmatched_confirmations` visible where the student
  will actually see it (a digest line, mirroring the existing `CONFIRMATIONS`
  section), not auto-creating a DRAFT drive from thin data.
- **This session's authorized scope already found a live-safety bug in the
  same code path** (Phase 2) from a single real sample. Building new
  creation logic on top of a matching/extraction path that just needed a
  real-sample-driven fix argues for letting this path season further (per
  the enforce-mode flip checklist's own "2-3 real samples" bar) before
  extending its responsibilities from "match an existing drive" to "decide
  when to create one."

### Recommendation: **not built.**

The n=1 gap size and the structural incompleteness of invite-sourced data
both argue against Phase 3 as scoped. If Ion Group (or a similarly-invited
company) recurs — either a broadcast eventually arrives and this analysis
should be revisited with 2+ real samples, or another invite-only company
shows up with no broadcast ever — that would be the point to reopen this
design question with a real base rate instead of one occurrence. In the
meantime, the smallest-value-per-line fix available is surfacing
`unmatched_confirmations` in the digest (not scoped to build here — this
doc's job was to investigate and recommend, not silently extend scope into
an adjacent fix).

**This is a CHECKPOINT, not a final answer** — if you want Phase 3 built
anyway (e.g. you value "some row over no row" more than this doc weighs it,
or you know a broadcast reliably never follows certain invite types), say so
and the shape described in the original task brief (DRAFT status,
company-normalization-based merge-on-broadcast-arrival, additive to
`unmatched_confirmations` not a replacement) is ready to implement against.
