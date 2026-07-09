"""Opt-in gate over the extraction-reliability eval suite (scripts/eval/).

Skipped by default (pyproject.toml: ``addopts = "-m 'not eval'"``). Run with:

    python -m pytest -m eval

This does NOT regenerate the corpus, cache, or score — it reads the
already-generated ``scripts/eval/out/score_report.json`` and checks each
field's precision/recall against a known-good floor, so a prompt/rule change
that regresses extraction quality fails a normal ``pytest -m eval`` run
instead of only being noticed by someone manually reading a printed table.

Regenerate score_report.json first:
    python scripts/eval/run_eval.py --extract --gemini-all --max-live-calls N
    python scripts/eval/run_eval.py --score
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SCORE_REPORT = (
    Path(__file__).resolve().parents[1] / "scripts" / "eval" / "out" / "score_report.json"
)

# Floors as of the last full corpus scoring in this repo's history (see
# docs/design/06-extraction-reliability.md for the full before/after story
# and why company/role/branches sit where they do). Update these only when a
# deliberate, eval-confirmed improvement (or an accepted, explained tradeoff)
# moves a field's numbers — not to silence a real regression.
MIN_PRECISION = {
    "company": 0.60,
    "role": 0.80,
    "deadline": 0.65,
    "oa_date": 0.60,
    "interview_date": 0.85,
    "cgpa_cutoff": 0.95,
    "branches": 0.95,
}

MIN_RECALL = {
    "company": 0.60,
    "role": 0.80,
    "deadline": 0.75,
    "oa_date": 0.65,
    "interview_date": 0.80,
    "cgpa_cutoff": 0.95,
    "branches": 0.95,
}


def _load_per_field() -> dict:
    if not SCORE_REPORT.exists():
        pytest.skip(
            f"{SCORE_REPORT} not found — run scripts/eval/run_eval.py "
            "--extract --gemini-all and --score first."
        )
    data = json.loads(SCORE_REPORT.read_text(encoding="utf-8"))
    return data["per_field"]


@pytest.mark.eval
class TestExtractionReliabilityFloors:
    @pytest.mark.parametrize("field", sorted(MIN_PRECISION))
    def test_precision_floor(self, field):
        per_field = _load_per_field()
        m = per_field[field]
        if not m["extracted_nonnull"]:
            pytest.skip(f"no non-null extractions for {field} in this score_report.json")
        precision = m["correct"] / m["extracted_nonnull"]
        assert precision >= MIN_PRECISION[field], (
            f"{field} precision {precision:.2f} fell below floor {MIN_PRECISION[field]:.2f}"
        )

    @pytest.mark.parametrize("field", sorted(MIN_RECALL))
    def test_recall_floor(self, field):
        per_field = _load_per_field()
        m = per_field[field]
        if not m["labeled_present"]:
            pytest.skip(f"no labeled ground truth for {field} in this score_report.json")
        recall = m["correct"] / m["labeled_present"]
        assert recall >= MIN_RECALL[field], (
            f"{field} recall {recall:.2f} fell below floor {MIN_RECALL[field]:.2f}"
        )
