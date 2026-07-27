"""One-off merge for pre-existing duplicate drives (doc 15 §1.5).

docs/design/15-roster-verification-and-confirmation-trust.md §1.3 found the
duplicate-drive bug is systemic: roughly 26-28 of 115 `opportunities` rows are
excess duplicates created before the matcher fix in
`utils/deduplication.py` and `db/manager.py::insert_or_update_opportunity`
(doc 15 §1.4 A-D). That fix only changes matching for *new* emails going
forward -- it does not retroactively collapse rows already sitting in the
table. This script does the one-off collapse.

Algorithm
---------
1. Load every row from the target `opportunities` table.
2. Union-find over all pairs using the same
   `utils.deduplication.compare_opportunities` the live pipeline now uses
   (identity + blockers + event coincidence, doc 15 §1.4). Only pairs where
   `is_duplicate` is True are unioned -- a `review_required` pair (identity
   matched but no event coincidence) is left alone and reported separately,
   never auto-merged.
3. For each cluster of size > 1, the "survivor" is the row with the most
   populated fields (ties broken by lowest id, i.e. the earliest-tracked
   drive). Every other row in the cluster is a "loser":
   - any field that is empty on the survivor and populated on the loser is
     copied onto the survivor (a COALESCE-style merge, same semantics as
     `_update_opportunity_row`'s live COALESCE behaviour);
   - `status_history` entries are unioned (order-preserving, capped);
   - an `updates` row is written on the survivor documenting the merge;
   - any `processed_emails` row pointing at the loser is re-pointed at the
     survivor (so email history for that drive stays queryable);
   - the loser row is deleted. `updates`/`sent_alerts`/`calendar_events` for
     the loser cascade-delete by FK; `processed_emails.opportunity_id`
     already got re-pointed above.

Safety
------
This script is dry-run by default: it prints the plan and writes nothing.
Pass `--apply` to actually write. It NEVER defaults to the live database --
`--db-path` is required, and it is meant to be pointed at an explicit copy
of `data/placement_mail_tracker.db` so the operator can diff before/after
and only then run it (with `--apply`) against the real file, on their own,
outside of any automated pipeline.

Usage
-----
    # 1. Make a copy and dry-run against it:
    cp data/placement_mail_tracker.db /tmp/tracker_copy.db
    python scripts/backfill_duplicate_opportunities.py --db-path /tmp/tracker_copy.db

    # 2. Once the plan looks right, apply it to the copy:
    python scripts/backfill_duplicate_opportunities.py --db-path /tmp/tracker_copy.db --apply

    # 3. Only after verifying the copy, the operator applies it to their own
    #    live database themselves -- this script does not do that for you.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from placement_mail_tracker.db.manager import (  # noqa: E402
    MAX_STATUS_HISTORY,
    DatabaseManager,
)
from placement_mail_tracker.utils.deduplication import (  # noqa: E402
    DeduplicationConfig,
    compare_opportunities,
)

logger = logging.getLogger(__name__)

# Fields eligible for the COALESCE-style fill-in-the-blanks merge. Deliberately
# excludes identity fields (company_name, role) and bookkeeping columns
# (id, unique_hash, drive_id, created_at, ...) which are never blindly copied.
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
)

_PLACEHOLDER_VALUES = {None, "", "[]", "Unknown Role", "UNKNOWN"}


class _UnionFind:
    def __init__(self, ids: list[int]) -> None:
        self._parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in self._parent:
            out.setdefault(self.find(i), []).append(i)
        return out


def _populated_field_count(row: dict[str, Any]) -> int:
    count = 0
    for field_name in _MERGE_FIELDS:
        value = row.get(field_name)
        if value in _PLACEHOLDER_VALUES:
            continue
        count += 1
    if row.get("role") not in _PLACEHOLDER_VALUES:
        count += 1
    return count


def _pick_survivor(cluster: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (survivor, losers) -- most populated fields, ties -> lowest id."""
    ordered = sorted(cluster, key=lambda r: (-_populated_field_count(r), r["id"]))
    return ordered[0], ordered[1:]


def _merge_status_history(survivor: dict[str, Any], loser: dict[str, Any]) -> list[str]:
    try:
        history_a = json.loads(survivor.get("status_history") or "[]")
    except (TypeError, ValueError):
        history_a = []
    try:
        history_b = json.loads(loser.get("status_history") or "[]")
    except (TypeError, ValueError):
        history_b = []
    merged: list[str] = []
    for status in [*history_a, *history_b]:
        if not merged or merged[-1] != status:
            merged.append(status)
    return merged[-MAX_STATUS_HISTORY:]


def find_duplicate_clusters(
    rows: list[dict[str, Any]],
    config: DeduplicationConfig | None = None,
) -> tuple[dict[int, list[int]], list[tuple[int, int, float]]]:
    """Return (merge-eligible clusters keyed by any member id, review-only pairs).

    Only pairs where ``compare_opportunities(...).is_duplicate`` is True are
    unioned into a mergeable cluster. Pairs that only reached
    ``review_required`` are reported back separately and never merged here
    (doc 15 §1.4-D: identity without event coincidence is a human-review
    case, not an auto-merge case, even during backfill).
    """
    if config is None:
        config = DeduplicationConfig()

    by_id = {row["id"]: row for row in rows}
    uf = _UnionFind(list(by_id))
    review_pairs: list[tuple[int, int, float]] = []

    ids = list(by_id)
    for i, id_a in enumerate(ids):
        for id_b in ids[i + 1 :]:
            result = compare_opportunities(by_id[id_a], by_id[id_b], config=config)
            if result.is_duplicate:
                uf.union(id_a, id_b)
            elif result.review_required:
                review_pairs.append((id_a, id_b, result.confidence_score))

    clusters = {root: members for root, members in uf.groups().items() if len(members) > 1}
    return clusters, review_pairs


def plan_merges(
    database: DatabaseManager,
    config: DeduplicationConfig | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[int, int, float]]]:
    """Build the merge plan without writing anything.

    Returns a list of per-cluster plan dicts and the list of review-only
    pairs found along the way (reported, never merged).
    """
    raw_rows = database.connection.execute("SELECT * FROM opportunities;").fetchall()
    rows = [dict(r) for r in raw_rows]
    clusters, review_pairs = find_duplicate_clusters(rows, config=config)

    by_id = {row["id"]: row for row in rows}
    plans: list[dict[str, Any]] = []
    for member_ids in clusters.values():
        cluster_rows = [by_id[i] for i in member_ids]
        survivor, losers = _pick_survivor(cluster_rows)
        field_updates: dict[str, Any] = {}
        for loser in losers:
            for field_name in _MERGE_FIELDS:
                if survivor.get(field_name) in _PLACEHOLDER_VALUES and loser.get(
                    field_name
                ) not in _PLACEHOLDER_VALUES:
                    field_updates.setdefault(field_name, loser.get(field_name))
        plans.append(
            {
                "survivor_id": survivor["id"],
                "survivor_company": survivor["company_name"],
                "survivor_role": survivor["role"],
                "loser_ids": [loser["id"] for loser in losers],
                "field_updates": field_updates,
            }
        )
    plans.sort(key=lambda p: p["survivor_id"])
    return plans, review_pairs


def apply_merges(
    database: DatabaseManager, config: DeduplicationConfig | None = None
) -> dict[str, int]:
    """Execute the merge plan against ``database``. Call only with intent to write."""
    raw_rows = database.connection.execute("SELECT * FROM opportunities;").fetchall()
    rows = [dict(r) for r in raw_rows]
    clusters, review_pairs = find_duplicate_clusters(rows, config=config)
    by_id = {row["id"]: row for row in rows}

    merged_count = 0
    for member_ids in clusters.values():
        cluster_rows = [by_id[i] for i in member_ids]
        survivor, losers = _pick_survivor(cluster_rows)
        survivor_id = survivor["id"]

        field_updates: dict[str, Any] = {}
        for loser in losers:
            for field_name in _MERGE_FIELDS:
                if survivor.get(field_name) in _PLACEHOLDER_VALUES and loser.get(
                    field_name
                ) not in _PLACEHOLDER_VALUES:
                    field_updates.setdefault(field_name, loser.get(field_name))

        running_history = survivor.get("status_history") or "[]"
        for loser in losers:
            running_history = json.dumps(
                _merge_status_history({"status_history": running_history}, loser)
            )
        merged_history = running_history

        set_clauses = [f"{name} = ?" for name in field_updates]
        params: list[Any] = list(field_updates.values())
        if merged_history is not None:
            set_clauses.append("status_history = ?")
            params.append(merged_history)
        if set_clauses:
            params.append(survivor_id)
            database.connection.execute(
                f"UPDATE opportunities SET {', '.join(set_clauses)} WHERE id = ?;",
                params,
            )

        for loser in losers:
            database.connection.execute(
                "UPDATE processed_emails SET opportunity_id = ? WHERE opportunity_id = ?;",
                (survivor_id, loser["id"]),
            )
            database.create_update_event(
                survivor_id,
                "merged",
                notes=(
                    f"Backfill merge (doc 15 §1.5): absorbed duplicate opportunity "
                    f"id={loser['id']} ({loser['company_name']!r} / {loser['role']!r})."
                ),
            )
            database.connection.execute("DELETE FROM opportunities WHERE id = ?;", (loser["id"],))
            merged_count += 1

    database.connection.commit()
    return {
        "clusters_merged": len(clusters),
        "rows_absorbed": merged_count,
        "review_pairs": len(review_pairs),
    }


def _print_plan(plans: list[dict[str, Any]], review_pairs: list[tuple[int, int, float]]) -> None:
    if not plans:
        print("No merge-eligible duplicate clusters found.")
    for plan in plans:
        print(
            f"MERGE: survivor id={plan['survivor_id']} "
            f"({plan['survivor_company']!r} / {plan['survivor_role']!r}) "
            f"<- losers {plan['loser_ids']}"
        )
        for field_name, value in plan["field_updates"].items():
            print(f"    fill {field_name} = {value!r} (from a loser row)")

    if review_pairs:
        print(f"\n{len(review_pairs)} pair(s) matched on identity but lack event "
              "coincidence -- NOT merged, needs human review:")
        for id_a, id_b, confidence in review_pairs:
            print(f"    id={id_a} vs id={id_b}  confidence={confidence}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-off merge of pre-existing duplicate opportunity rows under the "
            "fixed matching logic (docs/design/15-roster-verification-and-"
            "confirmation-trust.md §1.5). Dry-run by default."
        )
    )
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
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"Database not found: {args.db_path}", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.WARNING)
    database = DatabaseManager(args.db_path)

    if not args.apply:
        plans, review_pairs = plan_merges(database)
        _print_plan(plans, review_pairs)
        print(f"\nDRY RUN: {len(plans)} cluster(s) would be merged. Re-run with --apply to write.")
        return 0

    stats = apply_merges(database)
    print(
        f"Applied: {stats['clusters_merged']} cluster(s) merged, "
        f"{stats['rows_absorbed']} duplicate row(s) absorbed and deleted. "
        f"{stats['review_pairs']} pair(s) left for manual review (not merged)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
