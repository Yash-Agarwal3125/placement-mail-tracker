"""EVAL INSTRUMENTATION (temporary, not production code).

Rebuild the extraction-eval corpus by re-fetching every Gmail message the
tracker has ever seen (processed_emails + opportunities.source_email_id)
and storing one sanitized JSON fixture per mail under scripts/eval/corpus/.

Each fixture records BOTH what production extracted (body_text via
extract_body_text) and what production discarded (raw HTML part, attachment
inventory) so input-loss (T1) failures can be diagnosed.

Read-only everywhere: Gmail readonly scope, SELECT-only on the DB.

Usage (from repo root):
    python scripts/eval/fetch_corpus.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from placement_mail_tracker.config.settings import get_settings  # noqa: E402
from placement_mail_tracker.gmail.gmail_client import (  # noqa: E402
    GmailClient,
    decode_base64url,
    extract_body_text,
    get_header,
    normalize_gmail_timestamp,
)

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

# --- PII sanitization (other students' data must not persist in fixtures) ---
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?\b[6-9]\d{9}\b")
# VIT registration numbers, e.g. 22BCE1234 / 21BEC0042
_REGNO_RE = re.compile(r"\b\d{2}[A-Z]{3}\d{4}\b", re.IGNORECASE)


def sanitize(text: str) -> str:
    text = _EMAIL_RE.sub("<EMAIL>", text)
    text = _REGNO_RE.sub("<REGNO>", text)
    text = _PHONE_RE.sub("<PHONE>", text)
    return text


def _collect_message_ids(db_path: Path) -> dict[str, dict]:
    """Return {gmail_message_id: context} from processed_emails + opportunities."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ids: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT gmail_message_id, processed_status, email_classification,"
        " opportunity_id FROM processed_emails"
    ):
        ids[row["gmail_message_id"]] = {
            "processed_status": row["processed_status"],
            "email_classification": row["email_classification"],
            "opportunity_id": row["opportunity_id"],
        }
    for row in conn.execute(
        "SELECT source_email_id, id, drive_id FROM opportunities"
        " WHERE source_email_id IS NOT NULL"
    ):
        ids.setdefault(row["source_email_id"], {}).update(
            {"opportunity_id": row["id"], "drive_id": row["drive_id"]}
        )
    conn.close()
    return ids


def _part_inventory(payload: dict) -> tuple[list[dict], str | None, bool, bool]:
    """Walk the MIME tree; return (attachments, raw_html, has_plain, has_html)."""
    attachments: list[dict] = []
    html_chunks: list[str] = []
    has_plain = False
    has_html = False

    def walk(part: dict) -> None:
        nonlocal has_plain, has_html
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        filename = part.get("filename") or ""
        if body.get("attachmentId") or (filename and mime not in ("text/plain", "text/html")):
            attachments.append(
                {"filename": filename, "mimeType": mime, "size": body.get("size", 0)}
            )
        if mime == "text/plain" and body.get("data"):
            has_plain = True
        if mime == "text/html" and body.get("data"):
            has_html = True
            html_chunks.append(decode_base64url(body.get("data")))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    raw_html = "\n".join(html_chunks) if html_chunks else None
    return attachments, raw_html, has_plain, has_html


def main() -> int:
    settings = get_settings()
    ids = _collect_message_ids(settings.database_path)
    print(f"Message IDs known to the DB: {len(ids)}")
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    client = GmailClient(settings)
    service = client._get_service()  # noqa: SLF001 — eval tooling, read-only

    fetched, failed = 0, 0
    for msg_id, ctx in sorted(ids.items()):
        out_path = CORPUS_DIR / f"{msg_id}.json"
        if out_path.exists():
            continue
        try:
            raw = (
                service.users().messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except Exception as exc:  # keep going; some mails may be deleted
            print(f"  FETCH FAILED {msg_id}: {exc}")
            failed += 1
            continue

        payload = raw.get("payload", {})
        headers = payload.get("headers", [])
        attachments, raw_html, has_plain, has_html = _part_inventory(payload)
        body_text = extract_body_text(payload)  # exactly what production sees

        fixture = {
            "message_id": msg_id,
            "thread_id": raw.get("threadId", ""),
            "internal_date_ms": int(raw.get("internalDate", 0)),
            "date_header": get_header(headers, "Date"),
            "timestamp_iso": normalize_gmail_timestamp(get_header(headers, "Date")),
            "subject": sanitize(get_header(headers, "Subject", "(no subject)")),
            "sender": sanitize(get_header(headers, "From")),
            "db_context": ctx,
            "has_text_plain": has_plain,
            "has_text_html": has_html,
            "attachments": attachments,
            "body_text_production": sanitize(body_text),
            "body_html_raw": sanitize(raw_html) if raw_html else None,
        }
        out_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        fetched += 1
        time.sleep(0.1)  # gentle pacing; ~250 quota units/msg is nowhere near limits

    print(f"Fetched {fetched} new fixtures ({failed} failures); corpus at {CORPUS_DIR}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
