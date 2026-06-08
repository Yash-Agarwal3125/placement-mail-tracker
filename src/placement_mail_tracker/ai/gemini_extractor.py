"""Google Gemini extraction engine for placement email content."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from typing import Any, Protocol

from google import genai
from google.genai import errors as genai_errors
from pydantic import ValidationError

from placement_mail_tracker.ai.models import PlacementExtraction, empty_extraction_payload
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.gmail.gmail_client import GmailEmail

logger = logging.getLogger(__name__)

EXTRACTION_FIELDS = tuple(PlacementExtraction.model_fields.keys())
MAX_EMAIL_CHARS = 12000

SYSTEM_PROMPT = """You are an information extraction engine.

Extract placement and internship information from the email.

Return ONLY valid JSON.

If information is missing, use null.

Do not explain anything.
Do not include markdown.
Do not include code blocks.

Extract:
- company_name
- role
- opportunity_type
- stipend
- package
- location
- eligibility
- cgpa_requirement
- eligible_branches
- registration_deadline
- interview_date
- oa_date
- registration_link
- hiring_process
- important_notes
- update_type
- current_status
- action_required"""

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

FIELD_ALIASES = {
    "internship_or_fulltime": "opportunity_type",
    "package_or_stipend": "package",
    "branches_allowed": "eligible_branches",
    "deadline": "registration_deadline",
    "work_location": "location",
}


class GeminiModel(Protocol):
    """Small protocol for testable Gemini model clients."""

    def generate_content(self, prompt: str) -> Any:
        """Generate content from a prompt."""


class GeminiExtractionError(RuntimeError):
    """Raised when Gemini extraction repeatedly fails."""


class GeminiPlacementExtractor:
    """Extract structured placement details from raw email text using Gemini."""

    def __init__(
        self,
        settings: Settings,
        *,
        model: GeminiModel | None = None,
        max_retries: int = 6,  # 1 initial attempt + 5 retries
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self.settings = settings
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self._model = model
        self._client: genai.Client | None = None

    def extract_from_email(self, email: GmailEmail | dict[str, Any]) -> dict[str, Any]:
        """Extract structured placement fields from a Gmail email object or dict."""
        email_data = asdict(email) if isinstance(email, GmailEmail) else email
        subject = str(email_data.get("subject", ""))
        sender = str(email_data.get("sender", ""))
        body = str(email_data.get("body_text") or email_data.get("body") or "")

        return self.extract_from_raw_email(subject=subject, sender=sender, body=body)

    def extract_from_raw_email(
        self,
        *,
        subject: str = "",
        sender: str = "",
        body: str = "",
    ) -> dict[str, Any]:
        """Clean raw email fields and extract structured placement information."""
        content = clean_email_content(subject=subject, sender=sender, body=body)
        return self.extract_from_text(content)

    def extract_from_text(self, email_content: str) -> dict[str, Any]:
        """Send cleaned email content to Gemini and return validated dictionaries."""
        if not self.settings.gemini_api_key and self._model is None:
            logger.warning("Gemini API key is missing; returning empty extraction result")
            return empty_extraction_result()

        prompt = build_extraction_prompt(email_content)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                model_name = self.settings.gemini_model
                logger.info(
                    "Requesting Gemini placement extraction, attempt %s (model: %s)",
                    attempt,
                    model_name,
                )
                response = self._generate_content(prompt)
                raw_text = _response_text(response)
                parsed = parse_json_response(raw_text)
                return validate_extraction_result(parsed)
            except (
                GeminiExtractionError,
                ValidationError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                genai_errors.APIError,
            ) as error:
                last_error = error
                logger.warning("Gemini extraction attempt %s failed: %s", attempt, error)
                if attempt < self.max_retries:
                    backoff = 2**attempt
                    time.sleep(backoff)

        logger.error("Gemini extraction failed after %s attempts", self.max_retries)
        if last_error:
            raise GeminiExtractionError(str(last_error)) from last_error
        raise GeminiExtractionError("Unknown Gemini extraction failure")

    def _generate_content(self, prompt: str) -> Any:
        """Generate content using an injected fake model or the Gemini API."""
        if self._model is not None:
            return self._model.generate_content(prompt)

        if self._client is None:
            self._client = genai.Client(api_key=self.settings.gemini_api_key)

        model_name = self.settings.gemini_model

        return self._client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
            },
        )


def clean_email_content(*, subject: str, sender: str, body: str) -> str:
    """Remove disclaimers, signatures, quoted replies, and excess whitespace."""
    cleaned_body = _strip_quoted_replies(body)
    cleaned_body = _strip_patterns(cleaned_body, DISCLAIMER_PATTERNS)
    cleaned_body = _strip_patterns(cleaned_body, SIGNATURE_PATTERNS)
    cleaned_body = _normalize_whitespace(cleaned_body)

    cleaned = f"Subject: {subject}\nSender: {sender}\n\nEmail Body:\n{cleaned_body}"
    return cleaned[:MAX_EMAIL_CHARS]


def build_extraction_prompt(email_content: str) -> str:
    """Build an optimized strict JSON extraction prompt for Gemini."""
    keys = "\n".join(f'- "{field}"' for field in EXTRACTION_FIELDS)
    return f"""{SYSTEM_PROMPT}

JSON shape:
{{
{_json_shape_lines()}
}}

Additional rules:
- Use only the information present in the email.
- Use null for unknown scalar fields.
- Use null for unknown list fields instead of an empty list.
- eligible_branches, hiring_process, and important_notes must be JSON arrays when known.
- opportunity_type should be internship, fulltime, internship_and_fulltime, or null.
- update_type should summarize the email purpose, such as new_opportunity, deadline_update,
  shortlist, interview_update, oa_update, result_update, reminder, or null.
- current_status MUST be inferred from the email content. Valid values are exactly one of:
  NEW, PPT, OA, SHORTLISTED, INTERVIEW, HR, SELECTED, OFFER_RECEIVED, REJECTED.
- action_required should contain a brief sentence describing any action the user needs to take
  (e.g., "Submit resume", "Complete registration", "Attend interview"). Return null if no
  action is needed.
- Return exactly one JSON object with these keys and no extra keys:
{keys}

Email:
\"\"\"{email_content}\"\"\""""


def parse_json_response(raw_text: str) -> dict[str, Any]:
    """Parse Gemini text into JSON with cleanup and fallback extraction."""
    cleaned = clean_model_response(raw_text)

    try:
        loaded = json.loads(cleaned)
    except json.JSONDecodeError:
        loaded = fallback_parse_json_object(cleaned)

    if not isinstance(loaded, dict):
        msg = "Gemini response JSON must be an object"
        raise ValueError(msg)
    return _apply_field_aliases(loaded)


def clean_model_response(raw_text: str) -> str:
    """Remove markdown fences and obvious non-JSON text around a response."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def fallback_parse_json_object(text: str) -> dict[str, Any]:
    """Extract and parse the first JSON object from malformed surrounding text."""
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise json.JSONDecodeError("No JSON object found", text, 0)

    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(repaired)


def validate_extraction_result(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize Gemini output using Pydantic."""
    return PlacementExtraction.model_validate(_apply_field_aliases(data)).model_dump()


def empty_extraction_result() -> dict[str, Any]:
    """Return a complete empty extraction result."""
    return empty_extraction_payload()


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    msg = "Gemini response did not include text"
    raise GeminiExtractionError(msg)


def _apply_field_aliases(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for old_key, new_key in FIELD_ALIASES.items():
        if old_key in normalized and new_key not in normalized:
            normalized[new_key] = normalized[old_key]
    return normalized


def _json_shape_lines() -> str:
    return ",\n".join(f'  "{field}": null' for field in EXTRACTION_FIELDS)


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
