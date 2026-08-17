"""One-off merge for drives fragmented by role-hash instability (Cause 1,
calendar-drift remediation plan Phase 2, 2026-08-17).

``generate_unique_hash()`` keys a drive on company + role + type + year, and
``role`` is re-extracted per mail -- unstable enough that a single real drive
has minted 2-3 database rows over its lifetime (31 companies affected as of
the diagnosis). Phase 1 (``db/manager.py::insert_or_update_opportunity``)
stops new process mails from creating more of these; this script is the
one-off collapse of the rows that already exist.

Grouping, deliberately NOT identity-based
------------------------------------------
``scripts/backfill_duplicate_opportunities.py`` already does an identity-
based merge (``utils.deduplication.compare_opportunities``: company + role +
type fuzzy match). That is exactly the matcher that failed to catch these
rows in the first place -- the whole reason they fragmented is that ``role``
is too unstable to match on (e.g. ``role='s with the respective details
below'``). This script groups by **normalised company name + year** only,
which is coarse by design, and leans on human review of the printed
dry-run plan (roles, dates, source mail subjects for every member) to catch
the groups that are genuinely several concurrent drives at the same company
(Honeywell: 3; Flipkart: 2, plus GRiD 8.0 which is a contest, not a drive,
and is therefore excluded before grouping via ``drive_kind``).

Safety
------
Dry-run by default -- prints the plan, writes nothing. ``--apply`` is
required to write. ``--exclude-id`` (repeatable) lets the operator pull a
specific opportunity id out of every cluster after reviewing the dry-run
output, for the "this company genuinely runs several drives" case the plan
calls out. ``--db-path`` is required and is never the live database by
default; point it at a copy, review, then apply to the copy, then apply to
the live file yourself -- this script does not do that for you.

Every foreign key that points at a merged-away row is re-pointed, not
cascade-deleted: ``roster_verdicts``, ``calendar_events``, ``updates``,
``processed_emails``. Missing any one of these is exactly how the
calendar-drift bug class started (a verdict or event state left orphaned on
a row nothing points at any more).

Coalescing
----------
- Plain fields: most-recently-updated non-null value across the whole
  cluster (survivor included), per ``docs/design`` conventions the live
  ``_update_opportunity_row`` COALESCE path already uses.
- ``my_status``: highest rank on ``DatabaseManager.MY_STATUS_LADDER`` across
  the cluster -- never a downgrade.
- ``roster_verdicts``: one surviving row per ``event_type``, precedence
  ``MATCHED > NOT_MATCHED > AMBIGUOUS > NO_ROSTER`` (ties broken by most
  recent ``verified_at``). A ``method='user_asserted'`` row is dropped in
  favour of a real roster-derived verdict for the same drive+round when
  both exist in the cluster (the hardcoded-list verdicts from last week are
  pinned to opportunity ids this merge is about to retire).
- ``calendar_events``: one surviving state row per ``event_type`` (prefers
  ``status='active'``, then most recently updated) is re-pointed to the
  survivor; the rest are dropped -- a merged drive gets one canonical
  event per round going forward, rebuilt by the next normal ``sync()``.

Explicit ``connection.commit()`` at the end of ``apply_merges`` -- a
standalone script has nothing else in-process to flush the transaction
(the exact bug that cost a live run last week, per ``db/manager.py``'s
``mark_calendar_event_deleted`` docstring).

Usage
-----
    cp data/placement_mail_tracker.db /tmp/tracker_copy.db
    python scripts/backfill_drive_identity_merge.py --db-path /tmp/tracker_copy.db
    python scripts/backfill_drive_identity_merge.py --db-path /tmp/tracker_copy.db --apply
    python scripts/backfill_drive_identity_merge.py --db-path /tmp/tracker_copy.db \\
        --exclude-id 108 --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from placement_mail_tracker.db.manager import (  # noqa: E402
    MAX_STATUS_HISTORY,
    DatabaseManager,
)
from placement_mail_tracker.extraction.rule_engine import normalize_company_name  # noqa: E402
from placement_mail_tracker.utils.time import parse_datetime_flexible, utc_now_iso  # noqa: E402

logger = logging.getLogger(__name__)

# Same field set as backfill_duplicate_opportunities.py's _MERGE_FIELDS --
# identity fields (company_name, role) and bookkeeping columns are never
# blindly coalesced.
_MERGE_FIELDS = (
    "internship_or_fulltime",
    "package_or_stipend",
    "eligibility",
    "cgpa_requirement",
    "branches_allowed",
    "deadline",
    "interview_date",
    "oa_date",
    "registration_link",
    "work_location",
    "hiring_process",
    "important_notes",
    "source_thread_id",
    "degree_level",
    "dream_category",
    "eligibility_status",
    "priority",
    "drive_kind",
)

_PLACEHOLDER_VALUES = {None, "", "[]", "Unknown Role", "UNKNOWN"}

_VERDICT_PRECEDENCE = {"MATCHED": 3, "NOT_MATCHED": 2, "AMBIGUOUS": 1, "NO_ROSTER": 0}


def _row_year(row: dict[str, Any]) -> str:
    """Mirror ``db.manager._get_year_from_opportunity`` without importing a
    private helper across module boundaries."""
    received = row.get("email_received_at")
    if received:
        dt = parse_datetime_flexible(received)
        if dt is not None:
            return str(dt.year)
    created = row.get("created_at")
    if created:
        dt = parse_datetime_flexible(created)
        if dt is not None:
            return str(dt.year)
    return "unknown"


def _populated_field_count(row: dict[str, Any]) -> int:
    count = 0
    for field_name in _MERGE_FIELDS:
        if row.get(field_name) not in _PLACEHOLDER_VALUES:
            count += 1
    if row.get("role") not in _PLACEHOLDER_VALUES:
        count += 1
    return count


def _pick_survivor(cluster: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(survivor, losers) -- most populated fields, ties -> lowest id."""
    ordered = sorted(cluster, key=lambda r: (-_populated_field_count(r), r["id"]))
    return ordered[0], ordered[1:]


def _merge_status_history(members: list[dict[str, Any]]) -> list[str]:
    merged: list[str] = []
    for member in members:
        try:
            history = json.loads(member.get("status_history") or "[]")
        except (TypeError, ValueError):
            history = []
        for status in history:
            if not merged or merged[-1] != status:
                merged.append(status)
    return merged[-MAX_STATUS_HISTORY:]


def _coalesce_fields(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Most-recently-updated non-null value per field, across the cluster."""
    ranked = sorted(members, key=lambda r: r.get("updated_at") or "", reverse=True)
    updates: dict[str, Any] = {}
    for field_name in _MERGE_FIELDS:
        for member in ranked:
            value = member.get(field_name)
            if value not in _PLACEHOLDER_VALUES:
                updates[field_name] = value
                break
    return updates


def _coalesce_my_status(members: list[dict[str, Any]]) -> str:
    ladder = DatabaseManager.MY_STATUS_LADDER
    best = "NOT_APPLIED"
    best_rank = -1
    for member in members:
        status = member.get("my_status") or "NOT_APPLIED"
        rank = ladder.get(status, 0)
        if rank > best_rank:
            best_rank = rank
            best = status
    return best


def group_candidates(
    rows: list[dict[str, Any]], *, exclude_ids: set[int] | None = None
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group active, real-placement rows by (normalised company, year).

    Only ``status='active'`` rows are considered (a merge target that has
    already been soft-retired is not this script's concern), and only
    ``drive_kind='PLACEMENT'`` rows -- a company running a hackathon/contest
    alongside a real drive (Flipkart's GRiD 8.0) must not be pulled into the
    same cluster as the placement drive.
    """
    exclude_ids = exclude_ids or set()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("id") in exclude_ids:
            continue
        if row.get("status") != "active":
            continue
        if (row.get("drive_kind") or "PLACEMENT") != "PLACEMENT":
            continue
        norm_company = normalize_company_name(row.get("company_name"))
        if not norm_company:
            continue
        year = _row_year(row)
        groups[(norm_company, year)].append(row)
    return {key: members for key, members in groups.items() if len(members) > 1}


def _fetch_subjects(database: DatabaseManager, opportunity_id: int, limit: int = 3) -> list[str]:
    rows = database.connection.execute(
        """
        SELECT subject FROM processed_emails
        WHERE opportunity_id = ? ORDER BY received_at DESC LIMIT ?;
        """,
        (opportunity_id, limit),
    ).fetchall()
    return [r["subject"] for r in rows if r["subject"]]


def plan_merges(
    database: DatabaseManager, *, exclude_ids: set[int] | None = None
) -> list[dict[str, Any]]:
    """Build the merge plan without writing anything."""
    raw_rows = database.connection.execute("SELECT * FROM opportunities;").fetchall()
    rows = [dict(r) for r in raw_rows]
    groups = group_candidates(rows, exclude_ids=exclude_ids)

    plans: list[dict[str, Any]] = []
    for (norm_company, year), members in groups.items():
        survivor, losers = _pick_survivor(members)
        field_updates = _coalesce_fields(members)
        plans.append(
            {
                "company": norm_company,
                "year": year,
                "survivor_id": survivor["id"],
                "loser_ids": [m["id"] for m in losers],
                "field_updates": field_updates,
                "my_status": _coalesce_my_status(members),
                "members": [
                    {
                        "id": m["id"],
                        "role": m["role"],
                        "deadline": m.get("deadline"),
                        "oa_date": m.get("oa_date"),
                        "interview_date": m.get("interview_date"),
                        "subjects": _fetch_subjects(database, m["id"]),
                    }
                    for m in members
                ],
            }
        )
    plans.sort(key=lambda p: p["survivor_id"])
    return plans


def _coalesce_roster_verdicts(
    database: DatabaseManager, survivor_id: int, member_ids: list[int]
) -> None:
    rows = database.connection.execute(
        f"""
        SELECT * FROM roster_verdicts
        WHERE opportunity_id IN ({', '.join('?' for _ in member_ids)});
        """,
        member_ids,
    ).fetchall()
    by_event_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_event_type[row["event_type"]].append(dict(row))

    for event_type, verdict_rows in by_event_type.items():
        # Drop method='user_asserted' rows when a real roster-derived
        # verdict exists for this exact drive+round -- the hardcoded-list
        # verdicts are pinned to opportunity ids this merge retires.
        non_asserted = [r for r in verdict_rows if r["method"] != "user_asserted"]
        pool = non_asserted or verdict_rows
        winner = max(
            pool,
            key=lambda r: (_VERDICT_PRECEDENCE.get(r["verdict"], -1), r["verified_at"]),
        )

        # Clear every row for this event_type across the cluster first --
        # the (opportunity_id, event_type) primary key would otherwise
        # collide when the winner is re-pointed onto the survivor.
        database.connection.execute(
            f"""
            DELETE FROM roster_verdicts
            WHERE event_type = ?
              AND opportunity_id IN ({', '.join('?' for _ in member_ids)});
            """,
            (event_type, *member_ids),
        )
        database.connection.execute(
            """
            INSERT INTO roster_verdicts
                (opportunity_id, event_type, verdict, method, score, source_email_id, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                survivor_id,
                event_type,
                winner["verdict"],
                winner["method"],
                winner["score"],
                winner["source_email_id"],
                winner["verified_at"],
            ),
        )


def _coalesce_calendar_events(
    database: DatabaseManager, survivor_id: int, member_ids: list[int]
) -> None:
    rows = database.connection.execute(
        f"""
        SELECT * FROM calendar_events
        WHERE opportunity_id IN ({', '.join('?' for _ in member_ids)});
        """,
        member_ids,
    ).fetchall()
    by_event_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_event_type[row["event_type"]].append(dict(row))

    for event_type, state_rows in by_event_type.items():
        keeper = max(
            state_rows,
            key=lambda r: (r["status"] == "active", r.get("updated_at") or ""),
        )
        for row in state_rows:
            if row["id"] == keeper["id"]:
                database.connection.execute(
                    "UPDATE calendar_events SET opportunity_id = ? WHERE id = ?;",
                    (survivor_id, row["id"]),
                )
            else:
                # A dropped duplicate's Google event (if any) is left alone
                # on Google -- this script never calls the Calendar API.
                # The next normal sync() reconciles from the survivor's
                # single remaining state row.
                database.connection.execute(
                    "DELETE FROM calendar_events WHERE id = ?;", (row["id"],)
                )


def apply_merges(
    database: DatabaseManager, *, exclude_ids: set[int] | None = None
) -> dict[str, int]:
    """Execute the merge plan against ``database``. Call only with intent to write."""
    raw_rows = database.connection.execute("SELECT * FROM opportunities;").fetchall()
    rows = [dict(r) for r in raw_rows]
    groups = group_candidates(rows, exclude_ids=exclude_ids)

    merged_count = 0
    for (_norm_company, _year), members in groups.items():
        survivor, losers = _pick_survivor(members)
        survivor_id = survivor["id"]
        loser_ids = [m["id"] for m in losers]
        member_ids = [survivor_id, *loser_ids]

        field_updates = _coalesce_fields(members)
        field_updates["my_status"] = _coalesce_my_status(members)
        merged_history = json.dumps(_merge_status_history(members))

        set_clauses = [f"{name} = ?" for name in field_updates]
        params: list[Any] = list(field_updates.values())
        set_clauses.append("status_history = ?")
        params.append(merged_history)
        set_clauses.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(survivor_id)
        database.connection.execute(
            f"UPDATE opportunities SET {', '.join(set_clauses)} WHERE id = ?;",
            params,
        )

        # Re-point every foreign key -- never cascade-delete it away.
        _coalesce_roster_verdicts(database, survivor_id, member_ids)
        _coalesce_calendar_events(database, survivor_id, member_ids)
        database.connection.execute(
            f"""
            UPDATE updates SET opportunity_id = ?
            WHERE opportunity_id IN ({', '.join('?' for _ in loser_ids)});
            """,
            (survivor_id, *loser_ids),
        )
        database.connection.execute(
            f"""
            UPDATE processed_emails SET opportunity_id = ?
            WHERE opportunity_id IN ({', '.join('?' for _ in loser_ids)});
            """,
            (survivor_id, *loser_ids),
        )

        for loser in losers:
            database.create_update_event(
                survivor_id,
                "merged",
                notes=(
                    "Backfill merge (calendar-drift remediation plan Phase 2): "
                    f"absorbed duplicate opportunity id={loser['id']} "
                    f"({loser['company_name']!r} / {loser['role']!r})."
                ),
            )
            database.connection.execute(
                "DELETE FROM opportunities WHERE id = ?;", (loser["id"],)
            )
            merged_count += 1

    # Explicit commit: a standalone script has nothing else in-process to
    # flush the transaction (see mark_calendar_event_deleted's docstring
    # for the production incident this guards against).
    database.connection.commit()
    return {"clusters_merged": len(groups), "rows_absorbed": merged_count}


def _print_plan(plans: list[dict[str, Any]]) -> None:
    if not plans:
        print("No merge-eligible clusters found.")
        return
    for plan in plans:
        print(
            f"MERGE: {plan['company']!r} ({plan['year']}) -> survivor id="
            f"{plan['survivor_id']} <- losers {plan['loser_ids']} "
            f"(my_status={plan['my_status']})"
        )
        for member in plan["members"]:
            role = member["role"]
            dates = (
                f"deadline={member['deadline']!r} oa={member['oa_date']!r} "
                f"interview={member['interview_date']!r}"
            )
            subjects = "; ".join(member["subjects"]) or "(no processed_emails on file)"
            print(f"    id={member['id']} role={role!r} {dates}")
            print(f"        subjects: {subjects}")
        for field_name, value in plan["field_updates"].items():
            print(f"    coalesce {field_name} = {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        required=True,
        type=Path,
        help=(
            "Path to the SQLite database to operate on. Must be an explicit "
            "path -- there is no default, and this should be a copy of the "
            "live DB you have already made yourself, not the live file."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the merges. Without this flag, only the plan is printed.",
    )
    parser.add_argument(
        "--exclude-id",
        type=int,
        action="append",
        default=[],
        help=(
            "Opportunity id to pull out of every cluster before planning/applying "
            "(repeatable). Use after reviewing a dry-run to keep a genuinely "
            "distinct concurrent drive from being merged."
        ),
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"Database not found: {args.db_path}", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.WARNING)
    database = DatabaseManager(args.db_path)
    exclude_ids = set(args.exclude_id)

    if not args.apply:
        plans = plan_merges(database, exclude_ids=exclude_ids)
        _print_plan(plans)
        print(
            f"\nDRY RUN: {len(plans)} cluster(s) would be merged. "
            "Re-run with --apply to write."
        )
        return 0

    stats = apply_merges(database, exclude_ids=exclude_ids)
    print(
        f"Applied: {stats['clusters_merged']} cluster(s) merged, "
        f"{stats['rows_absorbed']} duplicate row(s) absorbed and deleted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
