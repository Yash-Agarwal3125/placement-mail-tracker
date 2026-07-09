"""Best-effort text extraction from email attachments, for the Gemini prompt.

Rules-based extraction never sees attachments (CLAUDE.md: deterministic
logic is the default, but there is no rule-based path for unstructured
attachment content). This module only feeds the Gemini fallback path, and
only when Gemini is already being called for a mail.

- .xlsx: parsed with the standard library only (zipfile + ElementTree pull
  the shared-strings table and sheet cell text directly out of the OOXML
  zip). No extra dependency needed for spreadsheets.
- .pdf: no stdlib text extraction exists, so this uses ``pypdf`` (a small,
  actively-maintained, pure-Python-ish library with no heavy native/OCR
  dependencies) — see requirements.txt.
- images: not parsed here at all. Image bytes are routed directly to Gemini
  as multimodal content parts by the caller (see ai/gemini_extractor.py);
  ``is_image_attachment`` just tells the caller which attachments those are.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from xml.etree import ElementTree as ET

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - dependency declared in requirements.txt
    PdfReader = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

# Per-attachment cap: keeps a single noisy attachment from crowding out the
# email body within MAX_EMAIL_CHARS (gemini_extractor.py).
MAX_ATTACHMENT_CHARS = 3000

_SHEET_NAME_RE = re.compile(r"^xl/worksheets/sheet\d+\.xml$")
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_XLSX_MIME_TYPES = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
)


def _localname(tag: str) -> str:
    return tag.rpartition("}")[2] if "}" in tag else tag


def _parse_shared_strings(data: bytes) -> list[str]:
    root = ET.fromstring(data)  # noqa: S314 - trusted own-account attachment, not arbitrary XML from the web
    strings: list[str] = []
    for si in root:
        if _localname(si.tag) != "si":
            continue
        text = "".join(node.text or "" for node in si.iter() if _localname(node.tag) == "t")
        strings.append(text)
    return strings


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if _localname(node.tag) == "t")

    value_node = next((child for child in cell if _localname(child.tag) == "v"), None)
    if value_node is None or value_node.text is None:
        return ""
    if cell_type == "s":
        try:
            return shared_strings[int(value_node.text)]
        except (ValueError, IndexError):
            return ""
    return value_node.text


def extract_xlsx_text(data: bytes, *, max_chars: int = MAX_ATTACHMENT_CHARS) -> str:
    """Pull readable cell text out of an .xlsx workbook using stdlib only.

    An .xlsx is a zip of OOXML parts: shared strings live in
    ``xl/sharedStrings.xml`` and cell values (or shared-string indices) live
    in ``xl/worksheets/sheetN.xml``. This walks every worksheet and joins
    non-empty cells per row, which is enough for Gemini to read a shortlist/
    opt-in table without a full spreadsheet-parsing dependency.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_strings = _parse_shared_strings(archive.read("xl/sharedStrings.xml"))

            sheet_names = sorted(n for n in names if _SHEET_NAME_RE.match(n))
            rows: list[str] = []
            for sheet_name in sheet_names:
                root = ET.fromstring(archive.read(sheet_name))  # noqa: S314
                for row in root.iter():
                    if _localname(row.tag) != "row":
                        continue
                    cells = [
                        _cell_text(cell, shared_strings)
                        for cell in row
                        if _localname(cell.tag) == "c"
                    ]
                    cells = [c for c in cells if c]
                    if cells:
                        rows.append(" | ".join(cells))
            text = "\n".join(rows)
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as error:
        logger.warning("Failed to parse .xlsx attachment: %s", error)
        return ""

    if len(text) > max_chars:
        logger.info(
            "Truncating .xlsx attachment text from %d to %d chars", len(text), max_chars
        )
        text = text[:max_chars]
    return text


def extract_pdf_text(data: bytes, *, max_chars: int = MAX_ATTACHMENT_CHARS) -> str:
    """Extract plain text from a text-based PDF (job descriptions, not scans)."""
    if PdfReader is None:  # pragma: no cover - dependency declared in requirements.txt
        logger.warning("pypdf is not installed; skipping .pdf attachment text extraction")
        return ""

    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as error:  # pypdf can raise several distinct error types on bad PDFs
        logger.warning("Failed to parse .pdf attachment: %s", error)
        return ""

    if len(text) > max_chars:
        logger.info(
            "Truncating .pdf attachment text from %d to %d chars", len(text), max_chars
        )
        text = text[:max_chars]
    return text


def is_image_attachment(filename: str, mime_type: str) -> bool:
    """True when an attachment should be routed to Gemini as an image part."""
    if (mime_type or "").lower().startswith("image/"):
        return True
    return (filename or "").lower().endswith(_IMAGE_EXTENSIONS)


def extract_attachment_text(filename: str, mime_type: str, data: bytes) -> str:
    """Dispatch attachment bytes to the right text extractor by name/MIME type.

    Returns "" for attachment kinds this module does not parse (e.g. images,
    which are routed to Gemini multimodal separately, or unsupported types).
    """
    name = (filename or "").lower()
    mime = (mime_type or "").lower()

    if name.endswith(".xlsx") or mime in _XLSX_MIME_TYPES:
        return extract_xlsx_text(data)
    if name.endswith(".pdf") or mime == "application/pdf":
        return extract_pdf_text(data)
    return ""
