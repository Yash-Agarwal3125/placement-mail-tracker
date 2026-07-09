"""EVAL INSTRUMENTATION (temporary, not production code).

Generate the ground-truth labeling sheet (labels.csv) from the replay output.

One row per mail x field, model-assisted prefill (Gemini-on-all result where
available, else what production stored). The USER is the ground truth:
- leave `corrected_label` empty to ACCEPT the prefill,
- write the true value in `corrected_label` to fix it (dates: YYYY-MM-DD or
  "YYYY-MM-DD HH:MM"; branches: comma-separated; empty prefill + empty
  correction = "field genuinely absent from this mail"),
- write NONE in `corrected_label` to say the prefill is wrong and the mail
  states no such value.

Usage: python scripts/eval/build_labels.py   (after run_eval.py --extract --gemini-all)
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

EVAL_DIR = Path(__file__).resolve().parent
OUT = EVAL_DIR / "out" / "extractions.json"
LABELS_CSV = EVAL_DIR / "labels.csv"

from run_eval import FIELD_TO_COLUMN, FIELD_TO_EXTRACTION, LABEL_FIELDS  # noqa: E402


def _prefill(field: str, rec: dict, final_row: dict) -> str:
    gem = rec.get("gemini_all_result") or {}
    ext_key = FIELD_TO_EXTRACTION[field]
    col = FIELD_TO_COLUMN[field]
    value = gem.get(ext_key) if ext_key else None
    if value in (None, "", []):
        value = final_row.get(col) if col else None
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def main() -> int:
    data = json.loads(OUT.read_text(encoding="utf-8"))
    final_by_id = {r["id"]: r for r in data["final_opportunities"]}

    rows = []
    n_mails = 0
    for rec in data["records"]:
        row_after = rec.get("opportunity_row_after")
        if not row_after:            # mail never produced/updated a drive: skip
            continue
        n_mails += 1
        final_row = final_by_id.get(row_after["id"], {})
        for field in LABEL_FIELDS:
            rows.append({
                "message_id": rec["message_id"],
                "received": rec["timestamp_iso"],
                "subject": rec["subject"][:90],
                "field": field,
                "rule_value": (rec["rule_result"].get(FIELD_TO_COLUMN[field] or "") or "")
                    if FIELD_TO_COLUMN[field] else "",
                "prefill_label": _prefill(field, rec, final_row),
                "corrected_label": "",
                "notes": "",
            })

    with LABELS_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {LABELS_CSV}: {len(rows)} rows ({n_mails} mails x {len(LABEL_FIELDS)} fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
