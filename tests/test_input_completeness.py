"""Regression tests for the input-completeness workstream.

Covers three independent fixes, each isolated so it can be verified without
a live Gmail/Gemini call:

(a) The mail's Date header (GmailEmail.timestamp) is threaded into the
    Gemini prompt as a "Received:" anchor, with an explicit prompt rule
    telling the model to resolve relative dates against it.
(b) Attachment text extraction: .xlsx (stdlib zipfile/ElementTree) and .pdf
    (pypdf) parsing, wired lazily behind GmailClient.fetch_attachment_bytes.
(c) Poster/screenshot image attachments are routed to Gemini as multimodal
    content parts only on the already-happening Gemini fallback call.
"""

from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from placement_mail_tracker.ai.attachments import (
    extract_attachment_text,
    extract_pdf_text,
    extract_xlsx_text,
    is_image_attachment,
)
from placement_mail_tracker.ai.gemini_extractor import (
    GeminiPlacementExtractor,
    build_extraction_prompt,
    clean_email_content,
)
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.gmail.gmail_client import (
    GmailEmail,
    decode_base64url_bytes,
    extract_attachment_parts,
)

# ---------------------------------------------------------------------------
# (a) Date-header anchor threaded into the Gemini prompt/content
# ---------------------------------------------------------------------------


class TestReceivedDateAnchor:
    def test_clean_email_content_includes_received_line(self):
        content = clean_email_content(
            subject="OA Update",
            sender="cdc@college.edu",
            body="Your OA is scheduled for this Friday.",
            received_at="2026-07-08T10:00:00+05:30",
        )
        assert "Received: 2026-07-08T10:00:00+05:30" in content
        assert content.index("Received:") > content.index("Sender:")
        assert content.index("Email Body:") > content.index("Received:")

    def test_clean_email_content_omits_received_line_when_absent(self):
        content = clean_email_content(
            subject="OA Update", sender="cdc@college.edu", body="Body text"
        )
        assert "Received:" not in content

    def test_build_extraction_prompt_has_relative_date_rule(self):
        prompt = build_extraction_prompt("Subject: X\nSender: Y\n\nEmail Body:\nZ")
        assert "Received:" in prompt
        assert "relative date" in prompt.lower()

    @pytest.fixture
    def test_settings(self):
        return Settings(
            app_env="testing",
            gemini_api_key="fake-key",
            gemini_model="gemini-2.5-flash",
            gemini_fallback_models=[],
            gemini_max_retries=1,
            gemini_max_models_to_try=1,
        )

    def test_extract_from_email_threads_timestamp_into_prompt(self, test_settings):
        """End-to-end (mocked model): the GmailEmail.timestamp value reaches
        the actual prompt text sent to the model, as a Received: anchor."""
        extractor = GeminiPlacementExtractor(test_settings)
        captured_prompts: list[str] = []

        def fake_generate(prompt: str) -> MagicMock:
            captured_prompts.append(prompt)
            return MagicMock(parsed=None, text='{"company_name": "Acme"}')

        extractor._model = MagicMock()
        extractor._model.generate_content.side_effect = fake_generate

        email = GmailEmail(
            message_id="m1",
            thread_id="t1",
            subject="OA Scheduled",
            sender="cdc@college.edu",
            timestamp="2026-07-08T10:00:00+05:30",
            body_text="Your OA is scheduled for this Friday at 2pm.",
            snippet="",
        )

        result = extractor.extract_from_email(email)

        assert result["company_name"] == "Acme"
        assert len(captured_prompts) == 1
        assert "Received: 2026-07-08T10:00:00+05:30" in captured_prompts[0]

    def test_extract_from_email_dict_input_also_threads_timestamp(self, test_settings):
        """The eval harness and the retry-queue path feed plain dicts (not
        GmailEmail instances); the timestamp key must still reach the prompt."""
        extractor = GeminiPlacementExtractor(test_settings)
        captured_prompts: list[str] = []

        def fake_generate(prompt: str) -> MagicMock:
            captured_prompts.append(prompt)
            return MagicMock(parsed=None, text="{}")

        extractor._model = MagicMock()
        extractor._model.generate_content.side_effect = fake_generate

        msg = {
            "subject": "Interview Update",
            "sender": "cdc@college.edu",
            "body_text": "Interview is tomorrow at 10am.",
            "timestamp": "2026-07-08T09:00:00+05:30",
        }
        extractor.extract_from_email(msg)

        assert "Received: 2026-07-08T09:00:00+05:30" in captured_prompts[0]


# ---------------------------------------------------------------------------
# (b) Attachment text extraction: .xlsx (stdlib) and .pdf (pypdf)
# ---------------------------------------------------------------------------


def _build_xlsx(rows: list[list[str]]) -> bytes:
    """Build a minimal, valid .xlsx (zip of OOXML parts) with one sheet."""
    strings = sorted({cell for row in rows for cell in row})
    index = {s: i for i, s in enumerate(strings)}

    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>"
    )
    row_xml = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(65 + c)}{r}" t="s"><v>{index[val]}</v></c>'
            for c, val in enumerate(row)
        )
        row_xml.append(f'<row r="{r}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def _build_minimal_pdf(text: str) -> bytes:
    """Build a minimal, well-formed single-page PDF containing ``text``."""
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >>"
        b" /MediaBox [0 0 200 200] /Contents 5 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    content = f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode()
    objs.append(b"5 0 obj\n<< /Length %d >>\nstream\n%s\nendstream\nendobj\n" % (
        len(content), content
    ))

    body = b"%PDF-1.4\n"
    offsets = []
    for obj in objs:
        offsets.append(len(body))
        body += obj
    xref_offset = len(body)
    xref = b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        xref += b"%010d 00000 n \n" % off
    trailer = b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1, xref_offset
    )
    return body + xref + trailer


class TestXlsxAttachmentExtraction:
    def test_extracts_shortlist_rows(self):
        data = _build_xlsx(
            [
                ["Roll Number", "OA Date"],
                ["21BCE1234", "2026-08-15"],
            ]
        )
        text = extract_xlsx_text(data)
        assert "Roll Number | OA Date" in text
        assert "21BCE1234 | 2026-08-15" in text

    def test_dispatches_by_filename_extension(self):
        data = _build_xlsx([["A", "B"]])
        text = extract_attachment_text("optin_list.xlsx", "application/octet-stream", data)
        assert "A | B" in text

    def test_dispatches_by_mime_type_without_extension(self):
        data = _build_xlsx([["X", "Y"]])
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        text = extract_attachment_text("attachment", mime, data)
        assert "X | Y" in text

    def test_malformed_xlsx_fails_soft(self):
        assert extract_xlsx_text(b"not a real zip file") == ""

    def test_truncates_long_xlsx_text(self):
        rows = [[f"cell{i}", f"value{i}"] for i in range(500)]
        data = _build_xlsx(rows)
        text = extract_xlsx_text(data, max_chars=100)
        assert len(text) == 100


class TestPdfAttachmentExtraction:
    def test_extracts_text_from_minimal_pdf(self):
        data = _build_minimal_pdf("Hello PDF")
        text = extract_pdf_text(data)
        assert "Hello PDF" in text

    def test_dispatches_by_filename_extension(self):
        data = _build_minimal_pdf("Job Description")
        text = extract_attachment_text("jd.pdf", "application/octet-stream", data)
        assert "Job Description" in text

    def test_malformed_pdf_fails_soft(self):
        assert extract_pdf_text(b"not a real pdf") == ""

    def test_truncates_long_pdf_text(self):
        data = _build_minimal_pdf("A" * 500)
        text = extract_pdf_text(data, max_chars=50)
        assert len(text) == 50


class TestImageAttachmentClassification:
    def test_image_mime_type_detected(self):
        assert is_image_attachment("poster", "image/png") is True

    def test_image_extension_detected_without_mime(self):
        assert is_image_attachment("poster.jpg", "") is True

    def test_xlsx_is_not_an_image(self):
        assert is_image_attachment("shortlist.xlsx", "application/octet-stream") is False

    def test_extract_attachment_text_returns_empty_for_images(self):
        # Images are routed to Gemini multimodal, not text-extracted here.
        assert extract_attachment_text("poster.png", "image/png", b"\x89PNG\r\n") == ""


# ---------------------------------------------------------------------------
# (b) Gmail-side wiring: attachment inventory + binary-safe byte decoding
# ---------------------------------------------------------------------------


class TestGmailAttachmentWiring:
    def test_extract_attachment_parts_finds_nested_attachment(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "aGVsbG8"}},
                {
                    "mimeType": "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet",
                    "filename": "shortlist.xlsx",
                    "body": {"attachmentId": "att123", "size": 4096},
                },
            ],
        }
        attachments = extract_attachment_parts(payload)
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "shortlist.xlsx"
        assert attachments[0]["attachmentId"] == "att123"
        assert attachments[0]["size"] == 4096

    def test_inline_text_parts_are_not_treated_as_attachments(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": "aGVsbG8="},
        }
        assert extract_attachment_parts(payload) == []

    def test_decode_base64url_bytes_is_binary_safe(self):
        import base64

        raw = bytes(range(256))  # includes bytes that are invalid UTF-8
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        decoded = decode_base64url_bytes(encoded)
        assert decoded == raw

    def test_decode_base64url_bytes_empty_input(self):
        assert decode_base64url_bytes(None) == b""
        assert decode_base64url_bytes("") == b""


# ---------------------------------------------------------------------------
# (b)/(c) Runner wiring: attachments fetched lazily only when Gemini runs
# ---------------------------------------------------------------------------


class TestRunnerAttachmentWiring:
    def _make_runner(self, mock_settings, db_manager):
        from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner

        return PlacementTrackerRunner(connection=db_manager.connection, settings=mock_settings)

    def test_prepare_attachments_returns_empty_when_no_gmail_client(
        self, mock_settings, db_manager
    ):
        runner = self._make_runner(mock_settings, db_manager)
        msg = {"attachments": [{"filename": "x.pdf", "attachmentId": "a1"}]}
        text, images = runner._prepare_attachments(None, msg)
        assert text == ""
        assert images == []

    def test_prepare_attachments_returns_empty_when_no_attachments(
        self, mock_settings, db_manager
    ):
        runner = self._make_runner(mock_settings, db_manager)
        gmail_client = MagicMock()
        text, images = runner._prepare_attachments(gmail_client, {"attachments": []})
        assert text == ""
        assert images == []
        gmail_client.fetch_attachment_bytes.assert_not_called()

    def test_prepare_attachments_extracts_xlsx_and_collects_images(
        self, mock_settings, db_manager
    ):
        runner = self._make_runner(mock_settings, db_manager)
        xlsx_bytes = _build_xlsx([["Roll", "OA Date"], ["21BCE0001", "2026-09-01"]])
        image_bytes = b"\xff\xd8\xff\xe0fakejpegdata"

        gmail_client = MagicMock()

        def fetch_side_effect(message_id, attachment_id):
            return {"a1": xlsx_bytes, "a2": image_bytes}[attachment_id]

        gmail_client.fetch_attachment_bytes.side_effect = fetch_side_effect

        msg = {
            "message_id": "m1",
            "attachments": [
                {
                    "filename": "shortlist.xlsx",
                    "mimeType": "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet",
                    "attachmentId": "a1",
                },
                {"filename": "poster.jpg", "mimeType": "image/jpeg", "attachmentId": "a2"},
            ],
        }

        text, images = runner._prepare_attachments(gmail_client, msg)

        assert "21BCE0001" in text
        assert "shortlist.xlsx" in text
        assert images == [(image_bytes, "image/jpeg")]

    def test_prepare_attachments_skips_attachment_on_fetch_failure(
        self, mock_settings, db_manager
    ):
        runner = self._make_runner(mock_settings, db_manager)
        gmail_client = MagicMock()
        gmail_client.fetch_attachment_bytes.side_effect = ConnectionError("network down")

        msg = {
            "message_id": "m1",
            "attachments": [{"filename": "x.pdf", "attachmentId": "a1"}],
        }
        text, images = runner._prepare_attachments(gmail_client, msg)
        assert text == ""
        assert images == []
