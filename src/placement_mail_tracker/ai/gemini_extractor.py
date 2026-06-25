"""Google Gemini extraction engine for placement email content."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from google import genai
from google.genai import errors as genai_errors
from pydantic import ValidationError

from placement_mail_tracker.ai.models import PlacementExtraction, empty_extraction_payload
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.gmail.gmail_client import GmailEmail

logger = logging.getLogger(__name__)

_HEALTH_FILE = Path("data/ai_model_health.json")

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
- action_required
- degree_level: "BTECH" if B.Tech only, "MTECH" if M.Tech only, "ANY" if both, null if unknown"""

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


def _backoff_secs(attempt: int) -> float:
    """Exponential backoff with jitter: 2^attempt + U(0,1), capped at 30 s."""
    return min(2 ** attempt + random.uniform(0, 1), 30.0)


def _load_health() -> dict[str, dict[str, int]]:
    try:
        if _HEALTH_FILE.exists():
            return json.loads(_HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_health(health: dict[str, dict[str, int]]) -> None:
    try:
        _HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HEALTH_FILE.write_text(json.dumps(health, indent=2), encoding="utf-8")
    except Exception:
        pass


def _record_health(
    health: dict[str, dict[str, int]],
    model: str,
    *,
    success: bool,
    quota_error: bool = False,
    is_fallback: bool = False,
) -> None:
    h = health.setdefault(
        model,
        {"requests": 0, "successes": 0, "failures": 0, "quota_errors": 0, "fallback_uses": 0},
    )
    h["requests"] += 1
    if success:
        h["successes"] += 1
    else:
        h["failures"] += 1
        if quota_error:
            h["quota_errors"] += 1
    if is_fallback:
        h["fallback_uses"] += 1


def _discover_available_models(client: genai.Client) -> None:
    """Log models available for this API key (best-effort, informational only)."""
    try:
        names = sorted(m.name for m in client.models.list() if hasattr(m, "name"))
        logger.info("[GEMINI] Models available in this API key: %s", ", ".join(names))
    except Exception as exc:
        logger.debug("[GEMINI] Could not list available models: %s", exc)


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
        max_retries: int | None = None,
        retry_delay_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.max_retries = max_retries if max_retries is not None else settings.gemini_max_retries
        self.retry_delay_seconds = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else settings.gemini_retry_delay_seconds
        )
        self._model = model
        self._client: genai.Client | None = None
        self._health: dict[str, dict[str, int]] = _load_health()
        self._models_discovered = False

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

        models_to_try = [self.settings.gemini_model] + self.settings.gemini_fallback_models

        for idx, model_name in enumerate(models_to_try):
            if idx == 0:
                logger.info("[GEMINI] Using model: %s", model_name)
            else:
                logger.info("[GEMINI] Switching to fallback model: %s", model_name)

            model_succeeded = False
            is_quota_err = False

            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.info(
                        "[GEMINI] Extraction attempt %s/%s (model: %s)",
                        attempt,
                        self.max_retries,
                        model_name,
                    )
                    response = self._generate_content(prompt, model_name)
                    raw_text = _response_text(response)
                    parsed = parse_json_response(raw_text)
                    result = validate_extraction_result(parsed)
                    model_succeeded = True
                    _record_health(self._health, model_name, success=True, is_fallback=idx > 0)
                    _save_health(self._health)
                    return result
                except (
                    GeminiExtractionError,
                    ValidationError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                    genai_errors.APIError,
                    ConnectionError,
                    TimeoutError,
                ) as error:
                    last_error = error
                    is_quota_err = isinstance(error, genai_errors.APIError) and error.code == 429
                    logger.warning("[GEMINI] Attempt %s failed: %s", attempt, error)

                    if attempt < self.max_retries:
                        if isinstance(error, genai_errors.APIError):
                            # Hard-stop codes: move directly to fallback model, no more retries.
                            if error.code in {400, 401, 403, 429}:
                                break
                        time.sleep(_backoff_secs(attempt))

            if not model_succeeded:
                _record_health(
                    self._health,
                    model_name,
                    success=False,
                    quota_error=is_quota_err,
                    is_fallback=idx > 0,
                )
                _save_health(self._health)

        logger.error("Gemini extraction failed after trying all fallback models")
        if last_error:
            raise GeminiExtractionError(str(last_error)) from last_error
        raise GeminiExtractionError("Unknown Gemini extraction failure")

    def _generate_content(self, prompt: str, model_name: str) -> Any:
        """Generate content using an injected fake model or the Gemini API."""
        if self._model is not None:
            return self._model.generate_content(prompt)

        if self._client is None:
            self._client = genai.Client(api_key=self.settings.gemini_api_key)
            if not self._models_discovered:
                self._models_discovered = True
                _discover_available_models(self._client)

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
- role is the job title / position being offered (e.g., "Software Engineer Intern",
  "Data Analyst"). Infer it from the subject or body; only use null if truly absent.
- eligible_branches, hiring_process, and important_notes must be JSON arrays when known.
- cgpa_requirement should be the minimum CGPA as a plain number string (e.g., "7.5"),
  or null if not stated.
- registration_deadline, interview_date, and oa_date MUST be returned in ISO 8601 format:
  "YYYY-MM-DD", or "YYYY-MM-DDTHH:MM" when a time is given. Convert any human-written date
  (e.g., "9 June 2026 (2 pm)") to this format. Use null if no date is present.
- opportunity_type should be internship, fulltime, internship_and_fulltime, or null.
- update_type should summarize the email purpose, such as new_opportunity, deadline_update,
  shortlist, interview_update, oa_update, result_update, reminder, or null.
- current_status MUST be inferred from the email content. Valid values are exactly one of:
  OPEN, REGISTERED, SHORTLISTED, OA, INTERVIEW, HR, SELECTED, OFFER_RECEIVED, REJECTED.
  Use OPEN for a newly announced drive or pre-placement talk.
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
