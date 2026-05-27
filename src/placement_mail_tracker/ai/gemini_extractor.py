"""Google Gemini extraction for structured placement information."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from typing import Any, Protocol

from google import genai

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.gmail.gmail_client import GmailEmail

logger = logging.getLogger(__name__)

EXTRACTION_FIELDS = (
    "company_name",
    "role",
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
)

LIST_FIELDS = {"branches_allowed", "hiring_process", "important_notes"}
MAX_EMAIL_CHARS = 12000

SIGNATURE_PATTERNS = (
    r"\n--\s*\n.*$",
    r"\nthanks\s*(and regards|& regards|,)?[\s\S]*$",
    r"\nregards,?[\s\S]*$",
    r"\nbest regards,?[\s\S]*$",
    r"\nsent from my .*$",
)

DISCLAIMER_PATTERNS = (
    r"(?is)this email and any attachments.*?(confidential|privileged).*?$",
    r"(?is)this message contains confidential information.*?$",
    r"(?is)please consider the environment before printing.*?$",
    r"(?is)you received this email because.*?(unsubscribe|manage preferences).*?$",
    r"(?is)to unsubscribe.*?$",
)


class GeminiModel(Protocol):
    """Small protocol for testable Gemini model clients."""

    def generate_content(self, prompt: str) -> Any:
        """Generate content from a prompt."""


class GeminiExtractionError(RuntimeError):
    """Raised when Gemini extraction repeatedly fails."""


class GeminiPlacementExtractor:
    """Extract structured placement details from email text using Gemini."""

    def __init__(
        self,
        settings: Settings,
        *,
        model: GeminiModel | None = None,
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.settings = settings
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self._model = model
        self._client: genai.Client | None = None

    def extract_from_email(self, email: GmailEmail | dict[str, Any]) -> dict[str, Any]:
        """Extract structured placement fields from a Gmail email."""
        email_data = asdict(email) if isinstance(email, GmailEmail) else email
        subject = str(email_data.get("subject", ""))
        sender = str(email_data.get("sender", ""))
        body = str(email_data.get("body_text") or email_data.get("body") or "")

        content = clean_email_content(subject=subject, sender=sender, body=body)
        return self.extract_from_text(content)

    def extract_from_text(self, email_content: str) -> dict[str, Any]:
        """Send cleaned email content to Gemini and return validated data."""
        if not self.settings.gemini_api_key and self._model is None:
            logger.warning("Gemini API key is missing; returning empty extraction result")
            return empty_extraction_result()

        prompt = build_extraction_prompt(email_content)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info("Requesting Gemini placement extraction, attempt %s", attempt)
                response = self._generate_content(prompt)
                raw_text = _response_text(response)
                parsed = parse_json_response(raw_text)
                return validate_extraction_result(parsed)
            except (GeminiExtractionError, json.JSONDecodeError, TypeError, ValueError) as error:
                last_error = error
                logger.warning("Gemini extraction attempt %s failed: %s", attempt, error)
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay_seconds * attempt)

        logger.error("Gemini extraction failed after %s attempts", self.max_retries)
        if last_error:
            raise GeminiExtractionError(str(last_error)) from last_error
        raise GeminiExtractionError("Unknown Gemini extraction failure")

    def _generate_content(self, prompt: str) -> Any:
        if self._model is not None:
            return self._model.generate_content(prompt)

        if self._client is None:
            self._client = genai.Client(api_key=self.settings.gemini_api_key)

        return self._client.models.generate_content(
            model=self.settings.gemini_model,
            contents=prompt,
            config={
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        )


def clean_email_content(*, subject: str, sender: str, body: str) -> str:
    """Remove noisy email text before sending content to Gemini."""
    cleaned_body = _strip_quoted_replies(body)
    cleaned_body = _strip_patterns(cleaned_body, DISCLAIMER_PATTERNS)
    cleaned_body = _strip_patterns(cleaned_body, SIGNATURE_PATTERNS)
    cleaned_body = _normalize_whitespace(cleaned_body)

    cleaned = f"Subject: {subject}\nSender: {sender}\n\nEmail Body:\n{cleaned_body}"
    return cleaned[:MAX_EMAIL_CHARS]


def build_extraction_prompt(email_content: str) -> str:
    """Build a strict prompt that asks Gemini for JSON only."""
    fields = "\n".join(f'- "{field}"' for field in EXTRACTION_FIELDS)
    return f"""
You are an information extraction system for college placement and internship emails.

Extract only facts explicitly present in the email. Do not guess. If a field is absent,
use null. Return valid JSON only. Do not include Markdown, comments, explanations, or
extra keys.

Required JSON keys:
{fields}

Rules:
- company_name: hiring company name only.
- role: job title, internship title, or profile name.
- internship_or_fulltime: one of "internship", "fulltime", "internship_and_fulltime", or null.
- package_or_stipend: salary, CTC, stipend, PPO info, or null.
- eligibility: degree/year/academic eligibility text.
- cgpa_requirement: CGPA, CPI, percentage, or academic threshold.
- branches_allowed: JSON array of branch names or empty array.
- deadline: registration or application deadline with date/time if available.
- interview_date: interview date/time if available.
- oa_date: online assessment, OA, test, or coding test date/time if available.
- registration_link: URL or email registration instruction.
- work_location: job or internship location.
- hiring_process: JSON array of process steps in order.
- important_notes: JSON array of important constraints, documents, or instructions.

Handle messy formatting, forwarded email separators, bullets, tables pasted as text,
and mixed Hindi/English wording. Preserve the original meaning. Keep values concise.

Email:
\"\"\"{email_content}\"\"\"
""".strip()


def parse_json_response(raw_text: str) -> dict[str, Any]:
    """Parse Gemini text into JSON, tolerating code fences and surrounding text."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        loaded = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        loaded = json.loads(match.group(0))

    if not isinstance(loaded, dict):
        msg = "Gemini response JSON must be an object"
        raise ValueError(msg)
    return loaded


def validate_extraction_result(data: dict[str, Any]) -> dict[str, Any]:
    """Keep expected keys and normalize missing values."""
    result = empty_extraction_result()

    for field in EXTRACTION_FIELDS:
        value = data.get(field)

        if field in LIST_FIELDS:
            result[field] = _normalize_list(value)
            continue

        result[field] = _normalize_scalar(value)

    return result


def empty_extraction_result() -> dict[str, Any]:
    """Return a complete empty extraction result."""
    return {field: [] if field in LIST_FIELDS else None for field in EXTRACTION_FIELDS}


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    msg = "Gemini response did not include text"
    raise GeminiExtractionError(msg)


def _strip_quoted_replies(value: str) -> str:
    separators = (
        r"\nOn .+ wrote:\n",
        r"\nFrom:\s.+\nSent:\s.+\n",
        r"\n-{2,}\s*Forwarded message\s*-{2,}\n",
    )
    cleaned = value
    for separator in separators:
        cleaned = re.split(separator, cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    return cleaned


def _strip_patterns(value: str, patterns: tuple[str, ...]) -> str:
    cleaned = value
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    return cleaned


def _normalize_whitespace(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    compact_lines = [line for line in lines if line]
    return "\n".join(compact_lines)


def _normalize_scalar(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, list):
        value = ", ".join(str(item).strip() for item in value if str(item).strip())

    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
        return None
    return normalized


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
        return []

    return [item.strip() for item in re.split(r"[,;\n]", normalized) if item.strip()]
