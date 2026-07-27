# 15 — Roster verification, duplicate-drive matching, and the confirmation trust model

Status: **design only**. Nothing in this document has been implemented. It is
written to be handed to an implementation session.

Investigation date: 2026-07-27. All evidence below was taken from the live DB
(`data/placement_mail_tracker.db`, 115 `opportunities` rows, opened read-only)
and from live Gmail metadata reads on the monitored account.

Prior art: `08-confirmation-audit.md` (matching gaps), `10-confirmation-and-reminders.md`
(Feature 1, the `my_status` write path), `12-eligibility-invite-gap.md`.

> **Note on numbering.** `docs/design/13` and `14` do not exist on `main`, and
> neither does `11`. Work referred to elsewhere as "doc 14" is not on the main
> line. This document does not depend on it, but an implementer should not go
> looking for it.

---

## 0. Scope changes discovered during investigation

Four findings materially change what the implementation session needs to do.
Read these before the sections below.

1. **Attachment parsing already exists** and needs no new dependency —
   but it truncates at 3000 characters, which is disqualifying for rosters.
   See §2.
2. **There is no user identity to match against.** `UserProfile` has no name,
   no registration number, no codename. This is a prerequisite, not a detail.
   See §3.
3. **The duplicate-drive bug is not a fuzzy-threshold tuning problem.** It is a
   missing-value-treated-as-conflicting-value bug that fails by 0.01, and the
   true duplicate count is ~26–28 rows out of 115, not 2. See §1.
4. **Confirmation mail is addressed to the personal Gmail, not the monitored
   VIT inbox.** Any "is this addressed to me" check on `To:`/`Delivered-To:`
   against the monitored mailbox would reject every genuine confirmation.
   See §4.

---

## 1. Duplicate-drive fix

### 1.1 What actually happens

Both known cases have **byte-identical company names**. There is no whitespace,
casing, or legal-suffix problem in either pair.

| | Cloudsek id=1 | Cloudsek id=6 |
|---|---|---|
| `company_name` | `'Cloudsek'` | `'Cloudsek'` (identical) |
| `role` | `'Unknown Role'` | `'Unknown Role'` (identical) |
| `internship_or_fulltime` | `None` | `'fulltime'` |
| `oa_date` | `2026-07-01T15:00` | `2026-07-01T14:00` |
| `source_thread_id` | `19f188e3310fe90f` | `19f1959722431ada` |

| | Tube Products id=21 | Tube Products id=33 |
|---|---|---|
| `company_name` | `'Tube Products Of'` | `'Tube Products Of'` (identical) |
| `role` | `'Unknown Role'` | `'Unknown Role'` (identical) |
| `internship_or_fulltime` | `None` | `'fulltime'` |
| `interview_date` | `2026-07-07T13:00` | `2026-07-07T13:00` (identical) |
| `source_thread_id` | `19f26410d9120880` | `19f365da012dd957` |

Tube Products has an identical interview datetime to the minute. These are
unambiguously one drive each.

### 1.2 Why the three gates all miss

There is exactly one insertion path: `runner.py:722` →
`DatabaseManager.insert_or_update_opportunity`. `calendar_sync/` never inserts
opportunities — it only derives events *from* them. Every duplicate is
email→email.

**Gate 1 — thread id** (`db/manager.py:345-346`). Both pairs arrived on
different Gmail threads, so this misses. Note 106 of 115 rows have
`source_thread_id == source_email_id`; threading essentially never groups
these announcements.

**Gate 2 — exact hash** (`db/manager.py:349-350` → `find_duplicate_opportunity`
at `manager.py:731-737` → `generate_unique_hash` at `manager.py:77-93`):

```python
parts = [_normalize_key(company_name), _normalize_key(role),
         _normalize_key(internship_or_fulltime), year]
```

`_normalize_key(None) == ""` (`manager.py:1439-1443`). So:

```
sha256("cloudsek::unknown role::::2026")         = a3bdafdb6a7d2ba4…  → row 1
sha256("cloudsek::unknown role::fulltime::2026") = d02c15c18f5d7b22…  → row 6
```

Different hash → `existing is None` → `_insert_opportunity` (`manager.py:384`).
`generate_drive_id` (`manager.py:96-134`) appends `category.upper()[:6]`, which
is where the observed `_FULLTI` suffix comes from.

**Gate 3 — the fuzzy matcher is advisory only, and it also misses.**
`runner.py:658` calls `find_best_match`; on a hit (`runner.py:659-667`) it only
rewrites `opp_data["company_name"]` and `opp_data["role"]`. It **never copies
`internship_or_fulltime`**, so even a correct fuzzy hit still mints a new hash
and inserts a new row.

It did not hit anyway. `compare_opportunities`
(`utils/deduplication.py:463-514`) on the real rows:

```
Cloudsek  1 vs 6:   company=100.0  role=100.0  type=0.0  confidence=81.99  is_duplicate=False
TubeProd 21 vs 33:  company=100.0  role=100.0  type=0.0  confidence=81.99  is_duplicate=False
```

The arithmetic:

- `normalize_opportunity_type(None)` = `""`;
  `normalize_opportunity_type("fulltime")` = `"full_time"`
  (`deduplication.py:285`).
- Not equal → `fuzz.token_set_ratio("", "full_time")` = **0.0**
  (`deduplication.py:355-363`).
- Weighted (`deduplication.py:430-437`): `100×0.45 + 100×0.40 + 0×0.15 = 85.00`.
- Hard type gate (`deduplication.py:496-499`): `type_effective 0.0 < 75.0`
  → `confidence = min(85.00, 82.0 − 0.01)` = **81.99**.
- `81.99 >= 82.0` is False (`deduplication.py:501-505`) → not a duplicate.

**It fails by 0.01, because a missing value is scored as a conflicting value.**
`require_type_match=True` (`deduplication.py:79`) was written to stop
"internship vs full-time at the same company" merging. It has no NULL branch,
so an *unextracted* type is punished exactly as hard as a *contradictory* one.

### 1.3 This is systemic — the true count is ~26–28, not 2

Grouping all 115 `company_name` values through `normalize_company` yields
**17 clusters with >1 row, covering 39 rows → 22 excess rows**. Adding clusters
that normalization itself splits apart (mode C below) brings the true excess to
roughly **26–28 rows, about a quarter of the table**.

**Mode A — NULL type vs populated type, identical company and role.** Seven
pairs, every one scoring exactly `81.99` against the `82.0` threshold:

| pair | company | corroboration |
|---|---|---|
| 1 / 6 | Cloudsek | known |
| 21 / 33 | Tube Products Of | known |
| 67 / 74 | Zomato | `oa_date` **and** `interview_date` identical |
| 41 / 42 | Varroc Engineering | both `Unknown Role` |
| 52 / 111 | Couchbase | id 111 got `COUCHBASE_2026_UNKNOWN_ROLE_02` |
| 12 / 53 | Flender | `2026-07-08` vs mis-parsed `2023-07-08` |
| 64 / 89 | Tekion | NULL vs `internship_and_fulltime` |

**Mode B — `'Unknown Role'` placeholder vs a real role title.** Role threshold
is 80.0 (`deduplication.py:71`); `token_set_ratio("unknown role", <anything>)`
lands at 15–50. With corroborating dates: Infosys 28/30 (both OA `2026-07-22`),
UBS 3/11 (both interview `2026-07-10`), Groww 13/54 (both interview
`2026-07-10`), Ivanti 25/51, Resmed 5/36/95, Visteon 18/106, Valeo 20/88
(`Intern` vs `Regular Internship`, ratio 50.0), Danfoss 37/115 (ratio 36.4 →
confidence 74.55), Valuelabs 16/23/103, Varroc 17/41.

**Mode C — subject noise leaking into `company_name`.**
`deduplication.normalize_company` (`deduplication.py:261-272`) strips only
legal-suffix tokens. It does **not** strip label prefixes — the richer
`rule_engine.normalize_company_name` does, but the dedup module doesn't call
it. So these never even reach the same cluster:

- `'Varroc Engineering'` vs `'Join Immediately: Varroc Engineering'` vs
  `'Update: Varroc Engineering'` (ids 41/42/44/46 — one drive)
- `'Zomato'` vs `'Eternal (zomato)'` (67/74 vs 66)
- `'Rhogenites'` vs `'Rhogenites Biotech Dream'` (57 vs 99)
- `'VIT- Business Incubator'` vs `'VIT- Business Incubator (vittbi)'` (2 vs 48)
- ids 101 `'Location'` and 104 `'Location Chennai Office'` — extraction
  garbage, not companies at all

Compounding this: `find_all_matches` (`deduplication.py:551-556`) pre-filters
candidates to those sharing the **first character** of the normalized company.
`'Eternal (zomato)'` (E) can never be compared against `'Zomato'` (Z) at any
threshold.

**The real correlation is temporal, not channel-based.** One row is created by
an early low-information email (type NULL, role `Unknown Role`, often
`email_classification='IRRELEVANT'`); the second by a later, richer email on a
new thread. The discriminator is "how much did the extractor manage to pull
out", not "which source did it come from".

### 1.4 Proposed fix

**A. Never let a missing field vote.** In `_score_field` /
`compute_confidence_score`, when either side's normalized value is empty — or
role normalizes to the sentinel `unknown role` — drop that field from the
comparison and **redistribute its weight** across the fields that do have data.
Do not score it 0.0. Under this rule Cloudsek 1/6 becomes company 100 + role 100
over weight 0.85 → 100.0, comfortably a duplicate; likewise all of mode A and
mode B.

Correspondingly, `require_type_match` (`deduplication.py:496-499`) must fire
**only when both sides have a known and different canonical type**
(`internship` vs `full_time`). Absent-vs-present must never trigger it.

**B. Make the hash tolerant, or stop using it as the sole gate.** Prefer the
latter: keep the hash as a fast path, but make the fuzzy result *authoritative*
— when `find_best_match` returns a duplicate, `runner.py` should pass the
matched `opportunity_id` into an update instead of rewriting two display fields
and re-hashing. Fixing only `runner.py:666-667` to also copy
`internship_or_fulltime` would paper over these two cases but leaves the
structural bug in place.

**C. Route `deduplication.normalize_company` through
`rule_engine.normalize_company_name`** (`extraction/rule_engine.py:116`), which
already strips `Update:` / `Join Immediately:` prefixes and canonicalizes
aliases. Add `Eternal (Zomato) → Zomato` to `_CANONICAL_NAMES`. **Delete the
first-character prefilter** at `deduplication.py:551-556` — at 115 rows it is a
correctness bug, not an optimization.

**D. The discriminating key — two stages.**

1. *Identity:* canonical company + academic year. Necessary, never sufficient.
2. *Event coincidence:* merge only if one holds —
   - shared `source_thread_id`, **or**
   - any populated date field coincides to the minute (`oa_date`,
     `interview_date`, `deadline` — Tube Products `2026-07-07T13:00`,
     Zomato `2026-07-26T11:30`), **or**
   - same date field within ±6 h (catches Cloudsek 15:00 vs 14:00) and no
     contradicting discriminator below.

**Blockers — these must PREVENT a merge:**

- **Known-and-different canonical type.** `internship` vs `full_time` at the
  same company are separate drives. `internship_and_fulltime` must be treated
  as *compatible with both*, not as a third distinct value — it currently
  scores 50.0/21.1 against them (e.g. Valuelabs 16/23).
- **Known-and-different specific roles** — two named, non-placeholder roles
  fuzzing below threshold. `Unknown Role` is not a role and must never block.
- **Non-overlapping dates** beyond tolerance with no shared thread. Ivanti and
  Resmed each legitimately ran an internship drive and a later offer-stage
  announcement.
- **Different eligibility batch / `degree_level` / `branches_allowed`** when
  both are populated and disjoint. `branches_allowed` is `'[]'` on every
  duplicate row today, so it is inert — but honor it once populated.
- **Genuinely different events at the same "company".** ids 69/79
  (`Vellore Institute Of (VIT)`, tech-team recruitment vs a semester-abroad
  info session) are *not* duplicates and score 78.06 — close enough that
  naively dropping the threshold to 75 would wrongly merge them. This is
  precisely why date coincidence must be a **required conjunct**, not a score
  booster.

Order: **blockers first, then identity, then event coincidence.** Anything that
passes identity but has neither event coincidence nor a contradicting
discriminator goes to `unmatched_confirmations` (already 67 rows) for review —
never a silent insert and never a silent merge.

### 1.5 Two adjacent defects found on the way

- `_insert_opportunity` (`manager.py:1067-1072`) de-collides drive_ids with
  `drive_id LIKE 'PREFIX%'`. `_` is a single-char wildcard in SQL LIKE and every
  drive_id is underscore-delimited, so the count over-matches. Harmless today
  (no duplicate drive_ids exist) but it makes the `_02` suffixes
  non-deterministic. Escape with `LIKE ... ESCAPE`.
- **Upstream extraction is the amplifier.** `Unknown Role` appears on about half
  the duplicate rows, `internship_or_fulltime` is NULL on most first-sightings,
  and `company_name` is sometimes a subject fragment (`'Location'`, truncated
  `'Tube Products Of'`). Fixing the matcher merges these rows but does not stop
  bad values entering. Both need attention.
- **A backfill is required.** The matcher change does not retroactively collapse
  the ~26 existing duplicate rows. Ship a one-off merge script alongside it.

---

## 2. Attachment parsing for rosters

### 2.1 What already exists — do not rebuild it

`src/placement_mail_tracker/ai/attachments.py` already handles both formats,
**with no new dependency**:

- `.xlsx` — `extract_xlsx_text` (line 77) parses OOXML with stdlib `zipfile` +
  `xml.etree.ElementTree`, resolving the shared-strings table and `inlineStr`
  cells. No `openpyxl`, no `pandas`.
- `.pdf` — `extract_pdf_text` (line 121) uses `pypdf`, already in
  `requirements.txt` (installed: 6.14.2).
- `rapidfuzz` (3.14.5) is already installed for §3's matching.

Verified installed set contains **only** `pypdf` and `rapidfuzz` — there is no
`openpyxl`, `pandas`, `pdfplumber`, `PyMuPDF`, `pillow`, or any OCR stack.

### 2.2 The two blockers

**Blocker 1 — the 3000-character truncation cap.**
`MAX_ATTACHMENT_CHARS = 3000` (`attachments.py:36`) exists to stop one noisy
attachment crowding out the email body in the Gemini prompt window. Both
extractors truncate to it (`attachments.py:113-118`, `134-139`).

A roster of a few hundred students is far larger than 3000 characters. Under
truncation the tail of the roster is silently dropped — and a name that was
dropped is indistinguishable from a name that was absent. **For a shortlist
check, that converts a truncation into a wrong verdict.** Roster extraction
therefore needs its own uncapped call path. Keep the cap on the Gemini prompt
path; parameterize `max_chars` (both functions already accept it as a keyword)
and pass `None`/a large bound for roster parsing.

**Blocker 2 — it only runs on the Gemini path.** The module docstring is
explicit: it "only feeds the Gemini fallback path, and only when Gemini is
already being called for a mail." Roster verification must not inherit that
dependency. Per `CLAUDE.md`, deterministic logic is the default; a shortlist
decision should not be gated on an LLM call, on Gemini quota, or on the
model-fallback chain.

### 2.3 Design

Add a roster module that reuses the existing extractors as **text sources** and
does its own structured parsing:

1. Call `extract_xlsx_text` / `extract_pdf_text` with the cap lifted.
2. Parse the resulting line-oriented text into candidate roster entries.
   `extract_xlsx_text` already emits one row per line, cells joined by `" | "`
   (`attachments.py:107`) — that is a usable record separator for free. PDF text
   is looser; split on newlines and treat runs of 2+ spaces as column breaks.
3. Extract, per line, whatever of `{registration_no, name}` is present. The
   registration number is the high-value token because it has a rigid shape —
   VIT registration numbers look like `23BAI1234` / `23BCE0001` (two digits,
   three letters, four digits). A regex on that shape is far more reliable than
   name parsing and should be the primary key.
4. Return a structured `RosterEntry` list plus a **parse-quality signal**: rows
   parsed, rows that yielded a registration number, and whether extraction was
   truncated or empty.

### 2.4 Scanned / image PDFs — flag, do not OCR

`pypdf.extract_text()` returns `""` (or near-empty) for a scanned PDF. No OCR
dependency is installed, and adding one (`pytesseract` + a Tesseract binary, or
`PyMuPDF` rasterization) is a substantial, platform-specific scope increase on
Windows.

**Recommended: detect and flag, do not OCR.** If a roster attachment yields
below a floor of extractable text (suggest: < 50 chars, or zero registration-
number matches across the whole document), classify the roster as
`UNPARSEABLE` and route the drive to `ROSTER_UNVERIFIED` (§5) with a
notification telling the user to check manually. This fails safe by
construction: an unparseable roster can never produce a shortlist claim.

Note there *is* an existing image path — `is_image_attachment`
(`attachments.py:142`) routes image attachments to Gemini as multimodal parts.
If scanned rosters turn out to be common in practice, routing them to Gemini
multimodal is the cheaper second option than adding an OCR stack. **Do not
build either until a real scanned roster sample exists** — currently zero are
confirmed.

---

## 3. Identity matching

### 3.1 Prerequisite: there is nothing to match against

`UserProfile` (`config/user_profile.py:12`) has exactly five fields: `degree`,
`branch`, `campus`, `graduation_year`, `cgpa`. No name. No registration number.
No codename.

Add to `UserProfile` (and to `config/user_profile.json`):

```python
full_name: str                      # as printed on rosters
registration_no: str                # e.g. "23BAI1234" — the primary key
name_aliases: list[str] = []        # initials, reversed order, common misspellings
codenames: list[str] = []           # any CDC-assigned identifier
```

**Failure mode to handle explicitly:** `UserProfile.load` currently falls back
to a hardcoded default profile on a missing or unparseable file
(`user_profile.py:29-43`, `48-60`), warning loudly but continuing. That
behaviour is defensible for eligibility filtering. It is **not** defensible for
identity matching — matching the user against a *default* identity could
produce a shortlist verdict about a person who is not the user. If
`registration_no` is absent or the profile fell back to the default, roster
verification must **hard-disable itself** and emit `ROSTER_UNVERIFIED`, not
proceed with placeholder values.

### 3.2 Matching strategy — registration number first

Registration number is a rigid identifier and should carry the decision.

1. **Exact registration match** (after normalizing case and stripping
   whitespace/punctuation) → `MATCHED`, confidence 100. Done. Name is not
   consulted.
2. **No registration number anywhere in the roster** → fall back to name
   matching, but at a *higher* bar and never above `AMBIGUOUS` on its own.
3. **Name matching** uses `rapidfuzz`. Follow the precedent already set in
   `extraction/confirmation.py:165-166`: a conservative absolute threshold
   **plus a uniqueness margin over the runner-up**. That two-part test is the
   right shape here too and is already proven in this codebase.

Suggested constants, to be calibrated against real fixtures before enforcing:

```python
ROSTER_NAME_THRESHOLD = 92.0     # higher than confirmation's 90.0
ROSTER_UNIQUENESS_MARGIN = 8.0   # wider than confirmation's 5.0
```

Both are stricter than the confirmation path because a roster contains hundreds
of real student names — many genuinely similar — whereas the confirmation path
scores against a handful of company names. Use `token_sort_ratio` rather than
`partial_ratio`: Indian names appear in varying orders on rosters, and
`partial_ratio` has a known substring-collision failure in this codebase
(`confirmation.py:168-175` documents "SES" tying at 100 against "assessments").

### 3.3 The three outcomes — and the one that must never happen

| outcome | condition | resulting state |
|---|---|---|
| `MATCHED` | exact reg-no hit, or name ≥ threshold *and* margin clear | `VERIFIED_SHORTLISTED` |
| `NOT_MATCHED` | roster parsed cleanly, ≥1 reg-no found, user's reg-no absent | `VERIFIED_NOT_SHORTLISTED` |
| `AMBIGUOUS` | roster unparseable/truncated/empty, no reg-nos found, name hit below margin, or profile incomplete | `ROSTER_UNVERIFIED` |

**The hard rule: `AMBIGUOUS` must never resolve toward "shortlisted".** A false
positive here tells the user they are through when they are not — the worst
failure mode in the system, and one they may act on by not preparing for a
different drive. Every uncertainty must degrade to `ROSTER_UNVERIFIED`.

`NOT_MATCHED` is only safe to assert when the roster parsed cleanly **and**
yielded at least one registration number. A roster that parsed to zero
registration numbers proves nothing about the user's absence from it — that is
`AMBIGUOUS`, not `NOT_MATCHED`.

---

## 4. Confirmation-forwarding trust model

### 4.1 The actual chain (header evidence)

Established from three real messages read live via Gmail metadata
(`19f956ceffc6bb7a` Nutanix, `19f945657f439bda` LSEG, `19f8e017004635c1` Zluri).
All three are structurally identical. Hops bottom → top:

1. `Received: from substrate.office.com … by MA5PR01MB13696.INDPRD01.PROD.OUTLOOK.COM`
   — origin is VIT's Microsoft 365 / Azure Communication Services tenant.
2. `Received: from MA0PR01CU012.outbound.protection.outlook.com … by mx.google.com … `
   **`for <yashagarwal3125@gmail.com>`**, `Return-Path: <noreply.cdcinfo@vitstudent.ac.in>`,
   `Received-SPF: pass`.
3. `Delivered-To: yashagarwal3125@gmail.com`,
   `X-Forwarded-For: yashagarwal3125@gmail.com yash.agarwal2023a@vitstudent.ac.in`,
   `X-Forwarded-To: yash.agarwal2023a@vitstudent.ac.in`.
4. `Received: from mail-sor-f41.google.com … for <yash.agarwal2023a@vitstudent.ac.in>`,
   `Return-Path: <yashagarwal3125+caf_=yash.agarwal2023a=vitstudent.ac.in@gmail.com>`
   — the `+caf_` VERP address is Gmail's consumer auto-forward signature.
5. `Delivered-To: yash.agarwal2023a@vitstudent.ac.in` — the monitored inbox.

Content headers: `From:`/`Sender:`/`Reply-To:` all
`VIT - Soft Skill Assessments <noreply.cdcinfo@vitstudent.ac.in>`;
**`To: Yash Agarwal <yashagarwal3125@gmail.com>`**; no `Cc`, no `Resent-*`, no
`X-Original-To`, no `Envelope-To`, no `List-Id`, no `Precedence: bulk`.
`DKIM-Signature: d=vitstudent.ac.in; s=selector1-azurecomm-prod-net`.

**Verdict: (a) personal → forward → VIT.** CDC sends to the personal Gmail —
that is the address registered with the placement portal — and a Gmail
auto-forward rule on that account relays to the monitored VIT inbox. The
forwarding is *downstream* of CDC, not upstream.

### 4.2 What the pipeline must trust

**`To:` never contains the monitored account.** Any check shaped like
"`To`/`Delivered-To` == monitored mailbox" would reject every genuine
confirmation. Do not write one.

The personal address must be registered as a **configured identity of the
user**, alongside the VIT address:

```python
# in settings / user profile
user_email_identities: list[str] = [
    "yash.agarwal2023a@vitstudent.ac.in",
    "yashagarwal3125@gmail.com",
]
```

Authoritative "this confirmation is genuinely for this user" =
`X-Forwarded-For` (whose two tokens are exactly personal → monitored), or
equivalently the bottom-most `Delivered-To` and the envelope
`for <…>` in the Outlook→Gmail hop. Because CDC sends individually addressed
mail (no `List-Id`, no bulk precedence), `To:` is *also* a valid per-user
signal — but only after the authentication check below, and only when checked
against the identity list rather than the monitored mailbox.

### 4.3 Authentication — trust DKIM, never SPF

At the final hop into the monitored mailbox:

```
dkim=pass header.i=@vitstudent.ac.in header.s=selector1-azurecomm-prod-net
arc=pass (i=3 spf=pass spfdomain=vitstudent.ac.in dkim=pass dkdomain=vitstudent.ac.in
          dmarc=pass fromdomain=vitstudent.ac.in)
dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=vitstudent.ac.in
spf=pass smtp.mailfrom=yashagarwal3125+caf_=...@gmail.com   ← the FORWARDER, not CDC
```

`From:` can be trusted — but **only because DKIM `d=vitstudent.ac.in` is aligned
and survives forwarding**, not because of SPF. SPF at the final hop
authenticates the forwarder; that is a classic forwarding SPF break, papered
over by ARC. `Return-Path` at the top is `@gmail.com` and is useless for sender
attribution.

**Trust rule:** DKIM `d=` is `vitstudent.ac.in` **AND** `Authentication-Results`
shows `dmarc=pass`. Not SPF pass. Not `Return-Path` domain.

`dara=fail header.i=@vitstudent.ac.in` appears at the top hop — that is a Google
DARA signal about the forwarder, not a DMARC failure. Ignore it.

Today's gate is sender-string-only: `CONFIRMATION_SENDER =
"noreply.cdcinfo@vitstudent.ac.in"` compared by exact match
(`extraction/rule_engine.py:22`, `25-28`, used at `rule_engine.py:239` and
`gmail/filters.py:151`). A `From:` string is trivially spoofable in principle;
the DKIM/DMARC check above is what makes trusting it sound, and should be added
rather than assumed.

### 4.4 Two open items found here

- **Each confirmation appears twice in `processed_emails` under two different
  Gmail message IDs** (Nutanix: `19f956ce43551334` and `19f956ceffc6bb7a`; a
  live `messages().list` with `includeSpamTrash=True` returns only the latter,
  the former 404s). This is *not* the cause of the §1 duplicate drives — those
  trace to different source emails entirely — but it is an unexplained
  double-insert or stale-ID condition worth its own investigation.
- **A mail subjected "Meet Agent Smith — Your Personal AI Coding Coach" is
  stored with `sender = noreply.cdcinfo@vitstudent.ac.in` and classified
  `APPLICATION_CONFIRMATION`.** The sender-only gate let it through. That is a
  live false-positive path into automated status writes and argues directly for
  requiring a `CONFIRMED`-tier pattern match, not just the sender gate, before
  any write.

---

## 5. State machine change

### 5.1 What exists today

Two independent status fields on `opportunities`:

- **`current_status`** — drive-level. Set from broadcast mail; the extractor's
  vocabulary is `OPEN, REGISTERED, SHORTLISTED, OA, INTERVIEW, HR, SELECTED,
  OFFER_RECEIVED, REJECTED` (`ai/gemini_extractor.py:726`).
- **`my_status`** — user-level, `NOT NULL DEFAULT 'NOT_APPLIED'`
  (`db/manager.py:195`), written only through the single choke point
  `set_my_status` (`manager.py:556`), which enforces an upgrade-only ladder for
  `source="automation"` (`manager.py:548-554`):

  ```
  NOT_APPLIED 0 → APPLIED 1 → SHORTLISTED 2 → SELECTED/REJECTED 3
  ```

**The separation is better than the brief assumed.** Automation today writes
only `my_status="APPLIED"` (`runner.py:875`), and `SHORTLISTED` reaches
`my_status` only from the human sheet path. So the conflation is *not* in
`my_status`.

### 5.2 Where the conflation actually is

It is in the **readers**. Consumers treat drive-level `current_status` as if it
described the user:

- `utils/scoring.py:30` — `high_priority_statuses = {"SHORTLISTED", "OA",
  "INTERVIEW", "HR", "SELECTED", "OFFER_RECEIVED"}` applied to the drive's
  status, so a broadcast "shortlist announced" mail raises the drive's priority
  as though the user were on the list.
- `scheduler/digest_generator.py`, `scheduler/alert_generator.py:162`, and
  `calendar_sync/derive.py:220` each branch on one or the other; these need
  auditing for the same conflation.

So item 5 is **"fix the readers and add a rung"**, not "build a new state
machine".

### 5.3 Proposed change

**Add two rungs to `MY_STATUS_LADDER`** between `APPLIED` and `SELECTED`:

```python
MY_STATUS_LADDER = {
    "NOT_APPLIED": 0,
    "APPLIED": 1,
    "ROSTER_UNVERIFIED": 2,       # roster seen, verdict not established
    "SHORTLISTED": 3,             # human-asserted, via sheet (unchanged meaning)
    "VERIFIED_SHORTLISTED": 4,    # reg-no matched in a cleanly parsed roster
    "SELECTED": 5,
    "REJECTED": 5,
}
```

`VERIFIED_NOT_SHORTLISTED` deliberately does **not** go on this ladder — the
ladder is monotonic and a not-shortlisted verdict is not an advancement. Record
it in a separate nullable column, `roster_verdict TEXT`, together with the
evidence needed to explain the decision:

```
roster_verdict         TEXT   -- MATCHED | NOT_MATCHED | AMBIGUOUS | NULL
roster_verdict_method  TEXT   -- registration_no | name_fuzzy | none
roster_verdict_score   REAL   -- fuzzy score when method=name_fuzzy
roster_source_email_id TEXT   -- which mail carried the roster
roster_verified_at     TEXT   -- UTC ISO
```

**Distinguishing the two events the brief asks about:**

- *"PPT/OA scheduled broadcast received"* → sets **`current_status`** only
  (`OA`, `SHORTLISTED`, …). Never touches `my_status`. This is what happens
  today and stays.
- *"verified shortlisted via roster match"* → sets
  **`my_status = VERIFIED_SHORTLISTED`** via
  `set_my_status(..., source="automation")`, and populates `roster_verdict`.

**Reader fix:** every consumer that currently asks "is this drive shortlisted?"
must be changed to ask "is *my_status* at or above `VERIFIED_SHORTLISTED`?"
before telling the user anything about their own progression. `scoring.py:30`
is the first and clearest instance.

**Reuse the existing safety properties.** The upgrade-only ladder already makes
duplicate and late roster mails idempotent for free (`manager.py:572-581`,
documented as D6 in doc 10), and already prevents an automated write from
downgrading a human `SHORTLISTED` assertion. Both properties are exactly what
roster verification needs — do not build a parallel mechanism.

**Ship it behind the existing feature flag.** `config/settings.py:106-109`
already gates automation `my_status` writes OFF by default pending a real-sample
review. Roster verification should ride the same gate (or an adjacent one) and
default OFF until the fixtures in §6 pass.

---

## 6. Test and eval requirements

### 6.1 Fixtures needed

Existing test layout is flat `tests/test_*.py` with `conftest.py`; there is no
fixtures directory, and `scripts/eval/corpus/` is empty (gitignored). Roster
fixtures are binary and must be committed, so add `tests/fixtures/rosters/`.

**Roster attachments — build these, do not wait for real ones:**

| fixture | purpose |
|---|---|
| `roster_small.xlsx` | ~20 rows, reg-no + name columns, user present |
| `roster_large.xlsx` | ~400 rows, **user's row near the end** — this is the truncation regression test |
| `roster_absent.xlsx` | clean roster, reg-nos present, user genuinely absent |
| `roster_no_regno.xlsx` | names only, no registration column |
| `roster_text.pdf` | text-layer PDF, user present |
| `roster_scanned.pdf` | image-only PDF → must yield `AMBIGUOUS` |
| `roster_malformed.xlsx` | truncated/corrupt zip → must not raise |

Reuse redacted real names where possible; substitute the user's own reg-no with
a synthetic one matching the `\d{2}[A-Z]{3}\d{4}` shape so fixtures can be
committed without exposing classmates' data.

**Confirmation-email fixtures** — capture the three real messages already
identified (`19f956ceffc6bb7a`, `19f945657f439bda`, `19f8e017004635c1`) as
`.eml`/JSON header fixtures with the **full forwarding chain preserved**:
`Received` hops in order, `X-Forwarded-For`, `X-Forwarded-To`, `Delivered-To`,
`Return-Path` (both), `Authentication-Results`, `DKIM-Signature`. Redact nothing
except any credential material — the addresses *are* the test data.

Add two negative confirmation fixtures:
- the "Meet Agent Smith" mail (correct sender, not a real confirmation) — must
  **not** produce a status write;
- a synthetic mail with `From: noreply.cdcinfo@vitstudent.ac.in` but
  `dkim=fail` / no DKIM — must be rejected by the §4.3 trust rule.

**Duplicate-matching fixtures** — the seven mode-A pairs and a representative
set of mode-B/C clusters from §1.3, as `opportunities` row dicts. Include the
**negative** pair 69/79 (`Vellore Institute Of (VIT)`, score 78.06) that must
stay unmerged.

### 6.2 What the tests must assert

**No-false-positive-shortlist — the headline requirement.** Parameterize over
every roster fixture *except* the ones where the user is genuinely present, and
assert `my_status` never reaches `VERIFIED_SHORTLISTED`. Specifically:

- `roster_absent` → `VERIFIED_NOT_SHORTLISTED`, never `VERIFIED_SHORTLISTED`
- `roster_scanned`, `roster_malformed`, `roster_no_regno` → `ROSTER_UNVERIFIED`
- profile missing `registration_no`, or `UserProfile.load` fell back to the
  default → `ROSTER_UNVERIFIED`, verification hard-disabled
- name fuzzes at 91.0 against a 92.0 threshold → `ROSTER_UNVERIFIED`
- two roster entries within the uniqueness margin → `ROSTER_UNVERIFIED`

**Truncation regression.** `roster_large.xlsx` with the user's row past the
3000-char mark must still yield `MATCHED`. This test fails today by
construction and is the reason §2.2 exists — write it first.

**Trust model.** Assert a confirmation whose `To:` is the *personal* address is
accepted (this is the normal case, and a naive "To == monitored" check would
break it); assert the `dkim=fail` fixture is rejected; assert
`X-Forwarded-For` correctly resolves to the user for all three real samples.

**Duplicate matching.** All seven mode-A pairs merge; pair 69/79 does **not**
merge; a synthetic `internship` vs `full_time` pair at the same company on the
same date does **not** merge; `internship_and_fulltime` merges with each of
them. Assert the confidence arithmetic directly — the current bug is a 0.01
miss, so a test that only checks the boolean will not tell you how close you
are.

**State separation.** A broadcast "shortlist announced" mail must move
`current_status` and leave `my_status` untouched — this is the regression test
for §5.2 and it should be written before the reader fixes, so it goes red
first.

Follow the existing precedent in `tests/test_my_status_writeback.py` and
`tests/test_confirmation_backfill.py:90`
(`test_backfill_never_downgrades_shortlisted`) — the never-downgrade property is
already tested there and the new rungs must not break it.

### 6.3 Eval

The eval harness (`scripts/eval/run_eval.py`) is all-or-nothing and consumes
live Gemini quota. Roster verification is deterministic by design (§2.2), so
**it should be tested by pytest against committed fixtures, not by the eval
harness.** Do not add roster cases to the Gemini eval. The only eval-worthy
question is whether the extractor correctly identifies *which attachment is a
roster*, and that can wait until real samples accumulate.

---

## 7. Suggested implementation order

1. State-separation regression test (§6.2) — goes red, proves the conflation.
2. Duplicate-matching fix (§1.4 A–D) + the seven-pair test. Highest value,
   entirely self-contained, no new config.
3. One-off backfill/merge script for the ~26 existing duplicates (§1.5).
4. `UserProfile` identity fields + hard-disable-on-incomplete (§3.1).
5. Uncapped roster extraction path + roster parser (§2.3) + truncation test.
6. Identity matching with the three-outcome model (§3.3).
7. New ladder rungs and `roster_verdict` columns + migration (§5.3).
8. Reader fixes, starting at `scoring.py:30` (§5.2).
9. DKIM/DMARC trust check on the confirmation gate (§4.3).
10. Flip the feature flag only once §6.2's no-false-positive suite is green.

Steps 2 and 3 are independently shippable and do not depend on anything else in
this document.
