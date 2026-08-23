"""Read-only anomaly triage: find drives that look wrong, ask Groq to explain
why and suggest a fix, write a report. Never applies anything automatically.

Why read-only, deliberately: an LLM that autonomously rewrites
roster_verdicts/calendar_events is the same failure mode that deleted a real
Foodhub interview (a single wrong verdict, applied silently), just with a
bigger blast radius. The direct-DB-write guard in this environment exists
for exactly this reason -- don't route around it by having an LLM do the
writing instead. A human reviews the suggested SQL in the report and runs it
(or not) explicitly, the same way scripts/fix_opportunity_182_foodhub.py
works.

What counts as "looks wrong" (the candidates this script gathers):
  1. Pending lines in data/calendar_flags.json (calendar_sync's own
     anomalies: unparseable dates, dropped collisions, and -- as of the
     2026-08-23 fix -- an "Unknown"-company drive with a pending date).
  2. Active opportunities with company_name == "Unknown"/blank and a
     non-empty action_required, regardless of whether calendar_sync has
     flagged them yet.
  3. An OA/INTERVIEW-round mail (my_status past NOT_APPLIED) that left every
     date field null -- nothing for derive_events() to put on the calendar
     at all, the exact shape of today's Unilever PPT miss (a pre-placement
     talk date has no extraction rule capturing it into any field yet).
  4. A NOT_MATCHED roster_verdicts row on an opportunity whose current_status
     has advanced past the round that verdict is for (e.g. NOT_MATCHED at OA
     while current_status is already INTERVIEW/OFFER_RECEIVED) -- the exact
     shape of contradiction that both the Foodhub and Unilever bugs had.

Usage:
    python scripts/diagnose_anomalies_with_groq.py [--limit N]

Requires GROQ_API_KEY in .env (same setting the extraction pipeline uses).
Writes a timestamped report to data/anomaly_reports/.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from placement_mail_tracker.config.settings import Settings  # noqa: E402
from placement_mail_tracker.scheduler.calendar_flags_store import (  # noqa: E402
    peek_pending_calendar_flags,
)

DB_PATH = ROOT / "data" / "placement_mail_tracker.db"
REPORT_DIR = ROOT / "data" / "anomaly_reports"

_PROMPT_TEMPLATE = """\
You are a debugging assistant for a personal placement-drive tracker (a \
SQLite database). Given a JSON description of one suspicious drive/anomaly, \
in 3-6 sentences: (1) explain in plain language what most likely went \
wrong, referencing the specific fields given, (2) state your confidence \
(high/medium/low), and (3) suggest ONE concrete, minimal SQLite statement \
(or short sequence) that a human could review and run to fix it -- or say \
"no safe fix without more information" if you're not confident. The row's \
own "source" field tells you which table it came from -- "row.id" in an \
"unknown_company_opportunity" or "contradictory_roster_verdict" candidate \
is opportunities.id (the table is named `opportunities`, not `drives`); a \
"contradictory_roster_verdict" candidate's fix targets the `roster_verdicts` \
table (primary key opportunity_id + event_type), not `opportunities`. Do \
not invent facts, column names, or table names not present in the data. \
Respond as plain text, not JSON.

Anomaly data:
{data}
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def gather_unknown_company_candidates(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, company_name, role, current_status, my_status, priority,
               action_required, deadline, oa_date, interview_date, next_event_date,
               drive_id, email_classification
        FROM opportunities
        WHERE status = 'active'
          AND (company_name IS NULL OR trim(company_name) = '' OR lower(company_name) = 'unknown')
        """
    ).fetchall()
    return [dict(r) for r in rows]


def gather_no_date_round_updates(con: sqlite3.Connection) -> list[dict]:
    """An OA/INTERVIEW-round mail that left every date field empty.

    This is the exact shape of today's Unilever miss: email_classification
    says a round event happened (OA_UPDATE/INTERVIEW_UPDATE), the user has
    engaged (APPLIED or further), but deadline/oa_date/interview_date/
    next_event_date are all still null -- meaning derive_events() has
    nothing to put on the calendar at all, regardless of roster/eligibility.
    Usually means the mail described an event type (e.g. a pre-placement
    talk) that has no extraction rule capturing its date into any of these
    fields yet.
    """
    rows = con.execute(
        """
        SELECT id, company_name, role, current_status, my_status, priority,
               action_required, email_classification, drive_id,
               email_received_at
        FROM opportunities
        WHERE status = 'active'
          AND email_classification IN ('OA_UPDATE', 'INTERVIEW_UPDATE')
          AND my_status != 'NOT_APPLIED'
          AND deadline IS NULL AND oa_date IS NULL
          AND interview_date IS NULL AND next_event_date IS NULL
        """
    ).fetchall()
    return [dict(r) for r in rows]


def gather_contradictory_roster_verdicts(con: sqlite3.Connection) -> list[dict]:
    """A NOT_MATCHED verdict on a round the drive has since visibly passed."""
    order = {"OA": 0, "INTERVIEW": 1}
    status_rank = {
        "OPEN": 0, "REGISTERED": 0, "OA": 1, "INTERVIEW": 2,
        "OFFER_RECEIVED": 3, "REJECTED": 3,
    }
    rows = con.execute(
        """
        SELECT rv.opportunity_id, rv.event_type, rv.verdict, rv.method, rv.verified_at,
               o.company_name, o.role, o.current_status, o.action_required
        FROM roster_verdicts rv JOIN opportunities o ON o.id = rv.opportunity_id
        WHERE rv.verdict = 'NOT_MATCHED' AND o.status = 'active'
        """
    ).fetchall()
    out = []
    for r in rows:
        round_idx = order.get(r["event_type"])
        current_idx = status_rank.get(r["current_status"] or "")
        if round_idx is not None and current_idx is not None and current_idx > round_idx:
            out.append(dict(r))
    return out


def call_groq(settings: Settings, data: dict) -> str:
    prompt = _PROMPT_TEMPLATE.format(data=json.dumps(data, indent=2, default=str))
    response = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        json={
            "model": settings.groq_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="Max candidates to send to Groq")
    args = parser.parse_args()

    settings = Settings()
    if not settings.groq_api_key:
        print("GROQ_API_KEY is not set in .env -- nothing to do.")
        return

    con = _connect()
    pending_flags = peek_pending_calendar_flags()
    unknown_company = gather_unknown_company_candidates(con)
    no_date_round_updates = gather_no_date_round_updates(con)
    contradictory_verdicts = gather_contradictory_roster_verdicts(con)
    con.close()

    candidates: list[dict] = []
    for line in pending_flags:
        candidates.append({"source": "calendar_flags.json", "line": line})
    for row in unknown_company:
        candidates.append({"source": "unknown_company_opportunity", "row": row})
    for row in no_date_round_updates:
        candidates.append({"source": "no_date_round_update", "row": row})
    for row in contradictory_verdicts:
        candidates.append({"source": "contradictory_roster_verdict", "row": row})

    if not candidates:
        print("No anomaly candidates found -- nothing to send to Groq.")
        return

    candidates = candidates[: args.limit]
    print(f"Found {len(candidates)} candidate(s) (capped at --limit {args.limit}). Asking Groq...")

    report_lines = [
        f"# Anomaly diagnosis report — {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        "",
        "Read-only: nothing below has been applied. Review each suggested fix",
        "before running it yourself (e.g. by hand, or by extending",
        "scripts/fix_opportunity_182_foodhub.py-style one-off scripts).",
        "",
    ]
    for i, candidate in enumerate(candidates, 1):
        report_lines.append(f"## Candidate {i}: {candidate['source']}")
        report_lines.append("")
        report_lines.append("```json")
        report_lines.append(json.dumps(candidate, indent=2, default=str))
        report_lines.append("```")
        report_lines.append("")
        try:
            explanation = call_groq(settings, candidate)
        except Exception as error:  # noqa: BLE001 - one bad candidate must not abort the run
            explanation = f"(Groq call failed: {error})"
        report_lines.append("**Groq's assessment:**")
        report_lines.append("")
        report_lines.append(explanation)
        report_lines.append("")
        print(f"[{i}/{len(candidates)}] {candidate['source']} -- diagnosed")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    main()
