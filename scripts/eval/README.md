# Extraction-reliability eval harness

Temporary instrumentation for diagnosing unreliable deadline / OA / interview /
eligibility extraction. **Nothing here runs in production**; the production DB
is never written (replays use an in-memory SQLite).

## Workflow

```powershell
python scripts/eval/fetch_corpus.py                 # 1. rebuild corpus from Gmail
python scripts/eval/run_eval.py --extract --gemini-all   # 2. replay prod path (Gemini cached)
python scripts/eval/build_labels.py                 # 3. generate labels.csv prefill
#    -> HUMAN step: correct labels.csv (corrected_label column) <-
python scripts/eval/run_eval.py --score             # 4. per-field P/R + miss list
```

## Artifacts (all git-ignored — contain real mail content)

- `corpus/<message_id>.json` — one fixture per mail: production-visible body
  text, raw HTML part, attachment inventory, headers. Emails/reg-nos/phones
  redacted at fetch time.
- `cache/gemini/<sha256>.json` — cached Gemini responses keyed by
  model+prompt hash. Reruns and `--score` need **no live API**.
- `labels.csv` — ground-truth sheet, one row per mail × field.
  `corrected_label`: empty = accept prefill; value = the truth; `NONE` =
  prefill wrong, mail states no such value.
- `out/extractions.json` — replay record per mail: rule result, whether the
  production path invoked Gemini, Gemini-on-all result, drive row at
  processing time and at end of replay (detects later erasure).
- `out/score_report.json` — per-field metrics + every miss with provenance
  hints for T1–T8 classification.

## Replay fidelity notes

- Mails are processed in received order through the real
  `PlacementTrackerRunner._process_single_message`, so filter gating,
  needs_gemini arbitration, the known-thread-followup Gemini skip, dedup and
  the update path behave exactly as production.
- The injected caching model uses the primary `GEMINI_MODEL` only (no
  fallback-model chain) at temperature 0 — deterministic and cache-stable.
