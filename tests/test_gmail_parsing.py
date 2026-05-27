"""Tests for Gmail message parsing helpers."""

import base64

from placement_mail_tracker.gmail.gmail_client import (
    extract_body_text,
    get_header,
    parse_gmail_message,
)


def encode_body(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("utf-8").rstrip("=")


def test_get_header_is_case_insensitive() -> None:
    headers = [{"name": "Subject", "value": "Campus hiring update"}]

    assert get_header(headers, "subject") == "Campus hiring update"


def test_extract_body_text_prefers_plain_text() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": encode_body("<p>Hello</p>")}},
            {"mimeType": "text/plain", "body": {"data": encode_body("Hello plain")}},
        ],
    }

    assert extract_body_text(payload) == "Hello plain"


def test_parse_gmail_message_extracts_required_fields() -> None:
    message = {
        "id": "msg-123",
        "threadId": "thread-456",
        "snippet": "Short preview",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Internship opportunity"},
                {"name": "From", "value": "TPO <tpo@example.com>"},
                {"name": "Date", "value": "Tue, 26 May 2026 12:00:00 +0530"},
            ],
            "body": {"data": encode_body("Apply before Friday.")},
        },
    }

    email = parse_gmail_message(message)

    assert email.message_id == "msg-123"
    assert email.subject == "Internship opportunity"
    assert email.sender == "TPO <tpo@example.com>"
    assert email.timestamp == "2026-05-26T12:00:00+05:30"
    assert email.body_text == "Apply before Friday."
