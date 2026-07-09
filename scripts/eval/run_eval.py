"""EVAL INSTRUMENTATION (temporary, not production code).

Replays the CURRENT production extraction path over the fixture corpus and
scores it against verified ground-truth labels.

Modes:
    python scripts/eval/run_eval.py --extract          # replay + record provenance
    python scripts/eval/run_eval.py --extract --gemini-all
        # additionally run Gemini on EVERY relevant mail (not just where
        # production would) so 'model wrong' (T2) can be separated from
        # 'fallback never triggered' (T6). Responses cached to disk — reruns
        # are free. Live call count is printed BEFORE any call is made and
        # calls are paced ~7s apart (free-tier RPM).
    python scripts/eval/run_eval.py --score            # compare out/extractions.json
                                                       # with labels.csv (verified)

Replay fidelity: fixtures are processed in received order through the real
``PlacementTrackerRunner._process_single_message`` against a throwaway
in-memory SQLite DB — so filter gating, rule/Gemini arbitration, the
known-thread-followup Gemini skip, dedup, and the (non-COALESCE) update path
all behave exactly as production. The production database is never touched.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

EVAL_DIR = Path(__file__).resolve().parent
CORPUS_DIR = EVAL_DIR / "corpus"
CACHE_DIR = EVAL_DIR / "cache" / "gemini"
OUT_DIR = EVAL_DIR / "out"
LABELS_CSV = EVAL_DIR / "labels.csv"

from placement_mail_tracker.ai.gemini_extractor import (  # noqa: E402
    GeminiPlacementExtractor,
    build_extraction_prompt,
    clean_email_content,
    parse_json_response,
    validate_extraction_result,
)
from placement_mail_tracker.config.settings import get_settings  # noqa: E402
from placement_mail_tracker.config.user_profile import UserProfile  # noqa: E402
from placement_mail_tracker.db.manager import DatabaseManager  # noqa: E402
from placement_mail_tracker.extraction.rule_engine import (  # noqa: E402
    extract_from_email as rule_extract,
)
from placement_mail_tracker.scheduler.runner import (  # noqa: E402
    PlacementTrackerRunner,
    map_extraction_to_opportunity,
)
from placement_mail_tracker.utils.time import parse_datetime_flexible  # noqa: E402

# Fields we label and score. visit_date and backlogs have no schema column yet;
# they are labeled anyway to quantify what the current schema cannot represent.
LABEL_FIELDS = (
    "company", "role", "deadline", "oa_date", "interview_date",
    "visit_date", "cgpa_cutoff", "branches", "backlogs",
)

# label field -> opportunities column (None = not representable today)
FIELD_TO_COLUMN = {
    "company": "company_name",
    "role": "role",
    "deadline": "deadline",
    "oa_date": "oa_date",
    "interview_date": "interview_date",
    "visit_date": None,
    "cgpa_cutoff": "cgpa_requirement",
    "branches": "branches_allowed",
    "backlogs": None,
}

# label field -> Gemini extraction key (post map_extraction_to_opportunity)
FIELD_TO_EXTRACTION = {
    "company": "company_name",
    "role": "role",
    "deadline": "deadline",
    "oa_date": "oa_date",
    "interview_date": "interview_date",
    "visit_date": None,
    "cgpa_cutoff": "cgpa_requirement",
    "branches": "branches_allowed",
    "backlogs": None,
}


class QuotaExhaustedError(RuntimeError):
    """Daily free-tier quota hit — the replay must abort, not degrade."""


class CachingGeminiModel:
    """Injectable Gemini model (GeminiModel protocol) with a disk cache.

    Cache key = sha256(model_name + prompt); identical prompts (production
    replay vs --gemini-all pass) share one cache entry, so each unique mail
    costs at most ONE live API call ever.

    Free-tier reality check (measured 2026-07-07): gemini-2.5-flash allows
    only 20 requests/day. ``max_live_calls`` budgets a run; a daily-quota 429
    sets ``quota_dead`` so the replay aborts instead of silently recording
    rule-only fallbacks that misrepresent production behaviour.
    """

    def __init__(
        self,
        settings: Any,
        pace_seconds: float = 7.0,
        *,
        model_name: str | None = None,
        max_live_calls: int = 18,
    ) -> None:
        self.settings = settings
        self.pace_seconds = pace_seconds
        self.model_name = model_name or settings.gemini_model
        self.max_live_calls = max_live_calls
        self.live_calls = 0
        self.cache_hits = 0
        self.quota_dead = False
        self._client = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, prompt: str, model_name: str | None = None) -> Path:
        digest = hashlib.sha256(
            f"{model_name or self.model_name}\n{prompt}".encode("utf-8")
        ).hexdigest()
        return CACHE_DIR / f"{digest}.json"

    #: models the eval may have cached under (free tier = 20/day each, so the
    #: corpus is necessarily built across several). Mixed-model cache is
    #: deliberate: production itself mixes models via its fallback chain.
    EVAL_MODELS = (
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash-lite",
    )

    def _cached(self, prompt: str) -> Path | None:
        """Find a cached response under the active model or any eval model."""
        names = dict.fromkeys(
            [self.model_name, self.settings.gemini_model, *self.EVAL_MODELS]
        )
        for name in names:
            path = self._cache_path(prompt, name)
            if path.exists():
                return path
        return None

    def generate_content(self, prompt: str) -> Any:
        path = self._cached(prompt)
        if path is not None:
            self.cache_hits += 1
            text = json.loads(path.read_text(encoding="utf-8"))["text"]
            return SimpleNamespace(text=text)
        path = self._cache_path(prompt)

        if self.quota_dead or self.live_calls >= self.max_live_calls:
            self.quota_dead = True
            raise QuotaExhaustedError("live-call budget exhausted")

        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.settings.gemini_api_key)

        if self.live_calls:
            time.sleep(self.pace_seconds)
        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config={"temperature": 0.0, "response_mime_type": "application/json"},
            )
        except Exception as exc:
            msg = str(exc)
            if "429" in msg and "PerDay" in msg:
                self.quota_dead = True
                raise QuotaExhaustedError(msg) from exc
            if "getaddrinfo" in msg or isinstance(exc, OSError):
                # network is gone — abort the replay rather than let every
                # remaining mail silently degrade to the rule-only path
                self.quota_dead = True
                raise QuotaExhaustedError(f"network failure: {msg}") from exc
            raise
        self.live_calls += 1
        text = getattr(response, "text", "") or ""
        path.write_text(json.dumps({"text": text}), encoding="utf-8")
        return SimpleNamespace(text=text)


def load_corpus() -> list[dict[str, Any]]:
    fixtures = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(CORPUS_DIR.glob("*.json"))
    ]
    fixtures.sort(key=lambda f: f.get("internal_date_ms", 0))
    if not fixtures:
        raise SystemExit("Corpus is empty — run fetch_corpus.py first.")
    return fixtures


def _fixture_to_msg(fx: dict[str, Any]) -> dict[str, Any]:
    """Shape a fixture like GmailClient's message dicts (runner input)."""
    return {
        "message_id": fx["message_id"],
        "thread_id": fx["thread_id"],
        "subject": fx["subject"],
        "sender": fx["sender"],
        "timestamp": fx["timestamp_iso"],
        "body_text": fx["body_text_production"],
        "snippet": "",
    }


def _row_snapshot(conn: sqlite3.Connection, msg_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT o.* FROM opportunities o JOIN processed_emails pe"
        " ON pe.opportunity_id = o.id WHERE pe.gmail_message_id = ?",
        (msg_id,),
    ).fetchone()
    return dict(row) if row else None


def run_extract(gemini_all: bool, model_name: str | None, max_live_calls: int) -> None:
    settings = get_settings()
    fixtures = load_corpus()

    caching_model = CachingGeminiModel(
        settings, model_name=model_name, max_live_calls=max_live_calls
    )
    # How many relevant mails do NOT have a cached response yet?
    uncached = 0
    relevant_estimate = 0
    for f in fixtures:
        if (f.get("db_context", {}) or {}).get("processed_status") != "processed":
            continue
        relevant_estimate += 1
        content = clean_email_content(
            subject=f["subject"], sender=f["sender"], body=f["body_text_production"]
        )
        if caching_model._cached(build_extraction_prompt(content)) is None:
            uncached += 1
    print(
        f"Corpus: {len(fixtures)} mails; ~{relevant_estimate} relevant; "
        f"{uncached} still uncached for model {caching_model.model_name}."
    )
    print(
        f"LIVE Gemini calls this run: min({uncached}, budget {max_live_calls}); "
        "pacing 7s/call. Aborts cleanly (no output written) if the daily quota dies."
    )
    if uncached > max_live_calls:
        print(
            f"NOTE: budget < uncached mails — this run CANNOT complete; "
            f"rerun on later days (cache persists) or raise --max-live-calls."
        )

    extractor = GeminiPlacementExtractor(settings, model=caching_model)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    database = DatabaseManager(connection=conn)
    runner = PlacementTrackerRunner(connection=conn, settings=settings)
    user_profile = UserProfile.load()

    records: list[dict[str, Any]] = []
    stats = {k: 0 for k in (
        "processed", "skipped", "errors", "gemini_calls", "rule_only", "created", "updated"
    )}

    aborted = False
    for fx in fixtures:
        msg = _fixture_to_msg(fx)
        rule_result = rule_extract(msg["subject"], msg["body_text"], msg["sender"])
        calls_before = caching_model.live_calls + caching_model.cache_hits

        runner._process_single_message(  # noqa: SLF001 — deliberate: replay the real path
            msg, database, extractor, user_profile, stats
        )

        if caching_model.quota_dead:
            # The in-flight mail took a degraded path; a partial replay would
            # misrepresent production. Abort without writing any output.
            aborted = True
            break

        gemini_used = (caching_model.live_calls + caching_model.cache_hits) > calls_before
        row_after = _row_snapshot(conn, fx["message_id"])

        record = {
            "message_id": fx["message_id"],
            "thread_id": fx["thread_id"],
            "subject": fx["subject"],
            "timestamp_iso": fx["timestamp_iso"],
            "has_text_plain": fx.get("has_text_plain"),
            "has_text_html": fx.get("has_text_html"),
            "attachments": fx.get("attachments", []),
            "body_chars": len(fx.get("body_text_production") or ""),
            "rule_result": rule_result.to_dict(),
            "rule_needs_gemini": rule_result.needs_gemini,
            "gemini_used_in_production_path": gemini_used,
            "gemini_all_result": None,
            "opportunity_row_after": row_after,
        }

        if gemini_all and row_after is not None:
            content = clean_email_content(
                subject=msg["subject"], sender=msg["sender"], body=msg["body_text"]
            )
            try:
                raw = caching_model.generate_content(build_extraction_prompt(content))
                parsed = validate_extraction_result(parse_json_response(raw.text))
                record["gemini_all_result"] = map_extraction_to_opportunity(parsed)
            except Exception as exc:
                record["gemini_all_result"] = {"__error__": str(exc)}

        records.append(record)

    if aborted:
        remaining = len(fixtures) - len(records)
        print(
            f"ABORTED on quota/budget exhaustion after {caching_model.live_calls} live "
            f"calls ({caching_model.cache_hits} cache hits); ~{remaining} mails left. "
            "No output written — rerun when quota allows; the cache persists."
        )
        raise SystemExit(2)

    final_rows = [dict(r) for r in conn.execute("SELECT * FROM opportunities").fetchall()]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "extractions.json").write_text(
        json.dumps(
            {"records": records, "final_opportunities": final_rows, "stats": stats},
            ensure_ascii=False, indent=1, default=str,
        ),
        encoding="utf-8",
    )
    print(
        f"Replay done: {stats} | live Gemini calls: {caching_model.live_calls}, "
        f"cache hits: {caching_model.cache_hits}"
    )
    print(f"Wrote {OUT_DIR / 'extractions.json'}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _norm_date(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M") if (dt.hour or dt.minute) else dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    dt = parse_datetime_flexible(raw)
    if dt is None:
        return f"UNPARSEABLE:{raw}"
    return dt.strftime("%Y-%m-%d %H:%M") if (dt.hour or dt.minute) else dt.strftime("%Y-%m-%d")


def _norm(field: str, value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    raw = str(value).strip()
    if not raw or raw.lower() in {"null", "none", "n/a", "na", "-", "[]"}:
        return None
    if field in {"deadline", "oa_date", "interview_date", "visit_date"}:
        return _norm_date(raw)
    if field == "cgpa_cutoff":
        try:
            return f"{float(raw.split()[0]):g}"
        except ValueError:
            return raw.casefold()
    if field == "branches":
        parts = sorted({p.strip().casefold() for p in raw.replace("/", ",").split(",") if p.strip()})
        return ", ".join(parts)
    return " ".join(raw.casefold().split())


def _dates_match(label_norm: str, got_norm: str) -> bool:
    """Match on calendar date; when BOTH carry a time, the time must match too."""
    ld, lt = (label_norm.split(" ") + [None])[:2]
    gd, gt = (got_norm.split(" ") + [None])[:2]
    if ld != gd:
        return False
    return lt is None or gt is None or lt == gt


def run_score() -> None:
    if not LABELS_CSV.exists():
        raise SystemExit(f"{LABELS_CSV} not found — labels must be verified first.")
    data = json.loads((OUT_DIR / "extractions.json").read_text(encoding="utf-8"))
    records = {r["message_id"]: r for r in data["records"]}
    final_by_id = {r["id"]: r for r in data["final_opportunities"]}

    labels: list[dict[str, str]] = []
    with LABELS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            labels.append(row)

    per_field: dict[str, dict[str, int]] = {
        f: {"labeled_present": 0, "extracted_nonnull": 0, "correct": 0,
            "wrong": 0, "missing": 0, "spurious": 0}
        for f in LABEL_FIELDS
    }
    misses: list[dict[str, Any]] = []

    for row in labels:
        field = row["field"]
        msg_id = row["message_id"]
        rec = records.get(msg_id)
        if rec is None or field not in per_field:
            continue
        label_raw = (row.get("corrected_label") or "").strip() or (row.get("prefill_label") or "").strip()
        label = _norm(field, label_raw)

        # what did the production pipeline finally store for this mail's drive?
        col = FIELD_TO_COLUMN[field]
        row_after = rec.get("opportunity_row_after") or {}
        opp_id = row_after.get("id")
        final_row = final_by_id.get(opp_id, {}) if opp_id else {}
        got = _norm(field, final_row.get(col)) if col else None
        got_at_time = _norm(field, row_after.get(col)) if col else None

        m = per_field[field]
        if label:
            m["labeled_present"] += 1
        if got:
            m["extracted_nonnull"] += 1

        is_date = field in {"deadline", "oa_date", "interview_date", "visit_date"}
        if label and got:
            ok = _dates_match(label, got) if is_date else (label == got)
            m["correct" if ok else "wrong"] += 1
            correct = ok
        elif label and not got:
            m["missing"] += 1
            correct = False
        elif got and not label:
            m["spurious"] += 1
            correct = False
        else:
            correct = True

        if not correct:
            gem = rec.get("gemini_all_result") or {}
            ext_key = FIELD_TO_EXTRACTION[field]
            body = ""  # populated from corpus for input-presence hint
            fx_path = CORPUS_DIR / f"{msg_id}.json"
            if fx_path.exists():
                body = json.loads(fx_path.read_text(encoding="utf-8")).get(
                    "body_text_production", "")
            misses.append({
                "message_id": msg_id,
                "subject": rec["subject"],
                "field": field,
                "label": label_raw,
                "stored_final": final_row.get(col) if col else None,
                "stored_at_processing_time": row_after.get(col) if col else None,
                "rule_value": rec["rule_result"].get(FIELD_TO_COLUMN[field] or "", None)
                    if FIELD_TO_COLUMN[field] else None,
                "gemini_all_value": gem.get(ext_key) if ext_key else None,
                "gemini_used_in_production": rec["gemini_used_in_production_path"],
                "attachments": rec.get("attachments"),
                "hints": {
                    "no_schema_column": col is None,
                    "value_tokens_in_body": _tokens_in(label_raw, body),
                    "erased_after_processing": bool(got_at_time) and not bool(got),
                    "gemini_all_had_it": bool(_norm(field, gem.get(ext_key))) if ext_key else False,
                },
                "taxonomy": "",   # filled during Phase-2 manual classification
            })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "score_report.json").write_text(
        json.dumps({"per_field": per_field, "misses": misses}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"{'field':<16}{'labeled':>8}{'extr.':>7}{'ok':>5}{'wrong':>7}{'miss':>6}"
          f"{'spur':>6}{'prec':>7}{'rec':>7}")
    for f, m in per_field.items():
        prec = m["correct"] / m["extracted_nonnull"] if m["extracted_nonnull"] else float("nan")
        rec_ = m["correct"] / m["labeled_present"] if m["labeled_present"] else float("nan")
        print(f"{f:<16}{m['labeled_present']:>8}{m['extracted_nonnull']:>7}{m['correct']:>5}"
              f"{m['wrong']:>7}{m['missing']:>6}{m['spurious']:>6}{prec:>7.2f}{rec_:>7.2f}")
    print(f"\n{len(misses)} field-level misses -> {OUT_DIR / 'score_report.json'}")


def _tokens_in(label_raw: str, body: str) -> bool:
    """Crude 'was the data present in the input text' hint for T1 triage."""
    if not label_raw or not body:
        return False
    body_cf = body.casefold()
    tokens = [t for t in label_raw.replace(",", " ").split() if len(t) >= 2]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t.casefold() in body_cf)
    return hits >= max(1, len(tokens) // 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--gemini-all", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--model", default=None, help="override GEMINI_MODEL for this run")
    ap.add_argument("--max-live-calls", type=int, default=18,
                    help="live API budget per run (free tier: 20/day/model)")
    args = ap.parse_args()
    if args.extract:
        run_extract(
            gemini_all=args.gemini_all,
            model_name=args.model,
            max_live_calls=args.max_live_calls,
        )
    if args.score:
        run_score()
    if not (args.extract or args.score):
        ap.print_help()


if __name__ == "__main__":
    main()
