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
from google.genai import types as genai_types
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
- degree_level: "BTECH" if B.Tech only, "MTECH" if M.Tech only, "ANY" if both, null if unknown
- confidence: your own self-assessed confidence, 0.0-1.0, that this whole extraction is
  fully correct and nothing was guessed. 1.0 = every field you filled in is explicitly
  stated in the email; lower it whenever you had to infer, resolve a relative date, or felt
  uncertain. Always include this field.

NEVER GUESS. If a field is not explicitly stated in the email (or cannot be resolved from an
explicit "Received:" anchor per the relative-date rule below), you MUST use null for it. Do
not invent a plausible-sounding company name, date, CGPA, or branch list. A null field is
correct and expected when the information genuinely is not in the email; a wrong guess is not."""

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


def _is_daily_quota_exhausted(error: Exception) -> bool:
    """Detect a genuine per-day quota 429, not a per-minute throttle or any
    other transient error. Mirrors the detection already used by the eval
    harness (scripts/eval/run_eval.py CachingGeminiModel.generate_content),
    which checks the same "429" + "PerDay" substrings in the error text.
    """
    msg = str(error)
    return "429" in msg and "PerDay" in msg


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


class GeminiQuotaExhaustedError(GeminiExtractionError):
    """Raised when every attempted model hit its own genuine per-day quota.

    Distinct from a generic ``GeminiExtractionError``: a per-day quota resets
    on its own schedule, so this is not a defect in the email or the model
    response. Callers should defer processing this email to a later run
    instead of silently degrading to the rule-only extraction and marking it
    "processed".
    """


class GeminiPlacementExtractor:
    """Extract structured placement details from raw email text using Gemini."""

    def __init__(
        self,
        settings: Settings,
        *,
        model: GeminiModel | None = None,
        max_retries: int | None = None,
        max_models_to_try: int | None = None,
        retry_delay_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.max_retries = max_retries if max_retries is not None else settings.gemini_max_retries
        self.max_models_to_try = (
            max_models_to_try
            if max_models_to_try is not None
            else settings.gemini_max_models_to_try
        )
        self.retry_delay_seconds = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else settings.gemini_retry_delay_seconds
        )
        self._model = model
        self._client: genai.Client | None = None
        self._health: dict[str, dict[str, int]] = _load_health()
        self._models_discovered = False

    def extract_from_email(
        self,
        email: GmailEmail | dict[str, Any],
        *,
        attachment_text: str = "",
        image_parts: list[tuple[bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """Extract structured placement fields from a Gmail email object or dict.

        ``attachment_text`` (pre-extracted .xlsx/.pdf text) and ``image_parts``
        (raw bytes + MIME type for poster/screenshot attachments) are optional
        enrichments the caller fetches lazily, only when Gemini is actually
        about to run on this mail (see scheduler/runner.py).
        """
        email_data = asdict(email) if isinstance(email, GmailEmail) else email
        subject = str(email_data.get("subject", ""))
        sender = str(email_data.get("sender", ""))
        body = str(email_data.get("body_text") or email_data.get("body") or "")
        received_at = str(email_data.get("timestamp") or email_data.get("received_at") or "")

        return self.extract_from_raw_email(
            subject=subject,
            sender=sender,
            body=body,
            received_at=received_at,
            attachment_text=attachment_text,
            image_parts=image_parts,
        )

    def extract_from_raw_email(
        self,
        *,
        subject: str = "",
        sender: str = "",
        body: str = "",
        received_at: str = "",
        attachment_text: str = "",
        image_parts: list[tuple[bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """Clean raw email fields and extract structured placement information."""
        content = clean_email_content(
            subject=subject,
            sender=sender,
            body=body,
            received_at=received_at,
            attachment_text=attachment_text,
        )
        return self.extract_from_text(content, image_parts=image_parts)

    def extract_from_text(
        self,
        email_content: str,
        *,
        image_parts: list[tuple[bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """Send cleaned email content to Gemini and return validated dictionaries."""
        if not self.settings.gemini_api_key and self._model is None:
            logger.warning("Gemini API key is missing; returning empty extraction result")
            return empty_extraction_result()

        prompt = build_extraction_prompt(email_content)
        last_error: Exception | None = None
        quota_exhausted_models = 0

        # Quota-aware cap: try at most `max_models_to_try` models (default 2:
        # primary + one fallback), each retried `max_retries` time(s) (default
        # 1). This bounds live calls per email to max_models_to_try *
        # max_retries instead of trying every configured fallback model.
        all_models = [self.settings.gemini_model] + self.settings.gemini_fallback_models
        models_to_try = all_models[: self.max_models_to_try]

        for idx, model_name in enumerate(models_to_try):
            if idx == 0:
                logger.info("[GEMINI] Using model: %s", model_name)
            else:
                logger.info("[GEMINI] Switching to fallback model: %s", model_name)

            model_succeeded = False
            is_quota_err = False
            is_daily_quota_err = False

            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.info(
                        "[GEMINI] Extraction attempt %s/%s (model: %s)",
                        attempt,
                        self.max_retries,
                        model_name,
                    )
                    # Only pass image_parts when there actually are images, so
                    # injected test/fake models (GeminiModel Protocol: a plain
                    # `generate_content(prompt)`) that don't accept this kwarg
                    # keep working unchanged for the (overwhelmingly common)
                    # no-image case.
                    response = (
                        self._generate_content(prompt, model_name, image_parts=image_parts)
                        if image_parts
                        else self._generate_content(prompt, model_name)
                    )
                    result = _extract_result_from_response(response)
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
                    is_daily_quota_err = _is_daily_quota_exhausted(error)
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
                if is_daily_quota_err:
                    quota_exhausted_models += 1

        logger.error("Gemini extraction failed after trying all fallback models")
        if models_to_try and quota_exhausted_models == len(models_to_try):
            # Every model we were allowed to try hit its own genuine per-day
            # quota (quota is per-model, not account-wide, so a fallback model
            # can still have headroom even when the primary is exhausted).
            # Only when *all* attempted models are exhausted is this a
            # today-only outage rather than an extraction failure.
            raise GeminiQuotaExhaustedError(str(last_error)) from last_error
        if last_error:
            raise GeminiExtractionError(str(last_error)) from last_error
        raise GeminiExtractionError("Unknown Gemini extraction failure")

    def _generate_content(
        self,
        prompt: str,
        model_name: str,
        *,
        image_parts: list[tuple[bytes, str]] | None = None,
    ) -> Any:
        """Generate content using an injected fake model or the Gemini API.

        ``image_parts`` (raw bytes + MIME type) are only honored on the real
        Gemini client path — the injected-model Protocol used by tests takes
        a plain string prompt, and this is the multimodal fallback path only
        (poster/screenshot attachments), not a new call added for every mail.
        """
        if self._model is not None:
            return self._model.generate_content(prompt)

        if self._client is None:
            self._client = genai.Client(api_key=self.settings.gemini_api_key)
            if not self._models_discovered:
                self._models_discovered = True
                _discover_available_models(self._client)

        contents: Any = prompt
        if image_parts:
            contents = [prompt] + [
                genai_types.Part.from_bytes(data=data, mime_type=mime_type)
                for data, mime_type in image_parts
            ]

        return self._client.models.generate_content(
            model=model_name,
            contents=contents,
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
                # Structured output: constrains the model to emit JSON that
                # conforms to PlacementExtraction's shape, and the SDK parses
                # + validates it into response.parsed for us (see
                # _extract_result_from_response), eliminating most of the
                # hand-rolled JSON-repair failure class at the source. The
                # prompt's own JSON instructions (build_extraction_prompt) are
                # kept as belt-and-suspenders.
                "response_schema": PlacementExtraction,
            },
        )


def clean_email_content(
    *,
    subject: str,
    sender: str,
    body: str,
    received_at: str = "",
    attachment_text: str = "",
) -> str:
    """Remove disclaimers, signatures, quoted replies, and excess whitespace.

    ``received_at`` (the mail's Date header, normalized to ISO 8601) is
    included as an explicit anchor so the model can resolve relative dates
    ("OA this Friday", "interview tomorrow") — see the matching rule in
    ``build_extraction_prompt``. ``attachment_text`` is pre-extracted text
    from .xlsx/.pdf attachments (ai/attachments.py), appended when present;
    image attachments are handled separately as Gemini multimodal parts, not
    as text here.
    """
    cleaned_body = _strip_quoted_replies(body)
    cleaned_body = _strip_patterns(cleaned_body, DISCLAIMER_PATTERNS)
    cleaned_body = _strip_patterns(cleaned_body, SIGNATURE_PATTERNS)
    cleaned_body = _normalize_whitespace(cleaned_body)

    header_lines = f"Subject: {subject}\nSender: {sender}"
    if received_at:
        header_lines += f"\nReceived: {received_at}"

    cleaned = f"{header_lines}\n\nEmail Body:\n{cleaned_body}"
    if attachment_text:
        cleaned += f"\n\nAttachment content:\n{attachment_text}"

    if len(cleaned) > MAX_EMAIL_CHARS:
        logger.info(
            "Email content (%d chars) exceeds MAX_EMAIL_CHARS (%d); truncating",
            len(cleaned),
            MAX_EMAIL_CHARS,
        )
    return cleaned[:MAX_EMAIL_CHARS]


# Few-shot examples grounded in real (PII-redacted) placement mail from
# scripts/eval/corpus/, cross-checked against scripts/eval/labels.csv ground
# truth. Email bodies are trimmed (institutional ranking bullets, the
# recurring disclaimer paragraph, and the CDC director's signature name
# removed) both to keep every future prompt call cheaper — this block is
# sent on every Gemini call, see cost principle #6 in CLAUDE.md — and to
# avoid embedding a real name in a prompt that isn't needed for the
# extraction task. Facts (company, dates, CGPA, branches, process steps) are
# unmodified from the source mail.
FEW_SHOT_EXAMPLES = """
Examples (email -> correct extraction):

Example 1 (new drive announcement; B.Tech branches list; registration_deadline
vs. interview_date both present and distinct):
Email:
\"\"\"Subject: Valuelabs LLP Super Dream Internship / Placements Registration - 2027 Batch
Sender: VIT Career Development Centre
Received: 2026-07-03T11:47:50+05:30

Email Body:
Valuelabs LLP Super Dream Internship / Placements Registration - 2027 Batch
Name of the Company: Valuelabs LLP
Category: Super Dream Internship / Placements Registration - 2027 Batch
Date of Visit: 16th & 17th July 2026 @ VIT Vellore Campus
Eligible Branches: B.Tech (CSE/IT) related branches only
Eligibility Criteria: 75% or 7.5 CGPA in X and XII, in pursuing degree, and in
UG (for PGs); no standing arrears
CTC: Year 1 (22 Lakhs): 16 Lakhs Fixed + 3 Lakhs Variable + 3 Lakhs Joining Bonus.
Year 2 (26 Lakhs): 18 Lakhs Fixed + 4 Lakhs Variable + 4 Lakhs Retention Bonus
Stipend: 50000
Last date for Registration: 04th July 2026 (10.00 am)
Website: www.valuelabs.com
Designation: Forward Deployed Engineer position
Location: Hyderabad (Work from Office)
Eligibility criteria: B.Tech/B.E. - CSE/IT/AI/ML/Python; strong technical
knowledge in AI/ML/Python/Java; 75% cut-off across all phases of academics
with no standing arrears
Plan for the Campus Drive: 1. Online Test 2. Pre-placement talk
3. Group Discussion 4. Level 1 interview - Technical
5. Level 2 interview - Technical 6. HR Interview
The campus recruitment drive will be conducted on 16th and 17th July 2026.
Students who do not have any courses (except capstone) in their 4th year alone
are eligible for the process, others please do not register.
All interested and eligible students should register in the NEO PAT on or
before 04th July 2026 (10.00 am). No manual registration or extension will be
entertained.\"\"\"
Correct output:
{
  "company_name": "Valuelabs LLP",
  "role": "Forward Deployed Engineer",
  "opportunity_type": "internship_and_fulltime",
  "stipend": "50000",
  "package": "Yr1: 22L CTC (16L fixed+3L var+3L bonus); Yr2: 26L CTC (18L fixed+4L var+4L bonus)",
  "location": "Hyderabad (Work from Office)",
  "eligibility": "75% or 7.5 CGPA in X, XII, and pursuing degree; no standing arrears",
  "cgpa_requirement": "7.5",
  "eligible_branches": ["CSE", "IT", "AI", "ML", "Python"],
  "registration_deadline": "2026-07-04T10:00",
  "interview_date": "2026-07-16",
  "oa_date": null,
  "registration_link": null,
  "hiring_process": [
    "Online Test", "Pre-placement talk", "Group Discussion",
    "Level 1 interview - Technical", "Level 2 interview - Technical", "HR Interview"
  ],
  "important_notes": [
    "No manual registration or extension will be entertained",
    "Students with pending courses in 4th year (except capstone) are not eligible to register"
  ],
  "update_type": "new_opportunity",
  "current_status": "OPEN",
  "action_required": "Register on the NEO PAT portal by 4 July 2026, 10:00 AM",
  "degree_level": "BTECH",
  "confidence": 0.9
}
(Note: "Website: www.valuelabs.com" is the company's site, not a registration
link, so registration_link is null rather than a guess.)

Example 2 (OA-date follow-up; ambiguous numeric date resolved day-month-year;
almost nothing else is stated, so almost everything else is null):
Email:
\"\"\"Subject: CloudSEK Online test is scheduled on 1-07-2026 by 3pm - Virtual mode @ Own location
Sender: VIT Career Development Centre
Received: 2026-06-30T18:16:16+05:30

Email Body:
CloudSEK Online test is scheduled on 1-07-2026 by 3pm - Virtual mode @ Own location
Find the below attached shortlist. Other details are sent to the shortlisted
candidates directly from the company.
Note: Shortlisted candidates who fail to take the test will be blacklisted
from further placements.
The webcam should be turned on till the test is over.\"\"\"
Correct output:
{
  "company_name": "CloudSEK",
  "role": null,
  "opportunity_type": null,
  "stipend": null,
  "package": null,
  "location": null,
  "eligibility": null,
  "cgpa_requirement": null,
  "eligible_branches": null,
  "registration_deadline": null,
  "interview_date": null,
  "oa_date": "2026-07-01T15:00",
  "registration_link": null,
  "hiring_process": null,
  "important_notes": [
    "Shortlisted candidates who fail to take the test will be blacklisted from further placements",
    "Webcam must stay on until the test is over"
  ],
  "update_type": "oa_update",
  "current_status": "OA",
  "action_required": "Take the online test on 1 July 2026 at 3:00 PM (virtual, own location)",
  "degree_level": null,
  "confidence": 0.85
}
(Note: "1-07-2026" is 1 July 2026 (day=1, month=07) under the day-month-year
convention, NOT January 7. role/opportunity_type/eligible_branches/etc. are
null because this follow-up email genuinely does not restate them - do not
carry over values from a previous email you have not seen.)

Example 3 (interview-date follow-up; distinguishes interview_date from
oa_date even though the subject just says "next round"):
Email:
\"\"\"Subject: Flender next round of selection process is scheduled on 08th & 9th July 2026
Sender: VIT Career Development Centre
Received: 2026-07-02T14:33:47+05:30

Email Body:
Flender next round of selection process is scheduled on 08th & 9th July 2026 -
Virtual mode @ Own location.
Please find the below shortlisted students list. The interview schedule, time
slot, and interview link will be shared directly with the candidates by the
company.
Note: Candidates who fail to attend the interview process will be blacklisted
from further placements.\"\"\"
Correct output:
{
  "company_name": "Flender",
  "role": null,
  "opportunity_type": null,
  "stipend": null,
  "package": null,
  "location": null,
  "eligibility": null,
  "cgpa_requirement": null,
  "eligible_branches": null,
  "registration_deadline": null,
  "interview_date": "2026-07-08",
  "oa_date": null,
  "registration_link": null,
  "hiring_process": null,
  "important_notes": [
    "Interview schedule, time slot, and link will be shared directly with shortlisted candidates",
    "Candidates who fail to attend will be blacklisted from further placements"
  ],
  "update_type": "interview_update",
  "current_status": "INTERVIEW",
  "action_required": "Attend the interview on 8-9 July 2026 (virtual, own location); await link",
  "degree_level": null,
  "confidence": 0.85
}
(Note: this is an "interview" round, not an "online test", so interview_date
is filled and oa_date stays null even though the subject only says "next
round of selection process".)

Example 4 (new drive announcement; M.Tech-only branches list):
Email:
\"\"\"Subject: Zetwerk Electronics Dream Core Internship Registration - 2027 Batch
Sender: VIT Career Development Centre
Received: 2026-07-03T10:42:01+05:30

Email Body:
Zetwerk Electronics Dream Core Internship Registration - 2027 Batch
Name of the Company: Zetwerk Electronics
Category: Dream Core Internship
Date of Visit: Will be announced later
Eligible Branches: M.Tech - Control and Automation, M.Tech - Mechatronics,
M.Tech - Power Electronics & Drives, M.Tech - Automotive Electronics,
M.Tech - Smart Manufacturing (Check the JD and apply)
Eligibility Criteria: 80% in X and XII or 8.0 CGPA; 70% in pursuing degree or
7.0 CGPA; no standing arrears
CTC: 5 LPA Fixed + 1 Lakh Retention Bonus (if converted)
Stipend: 15000
Last date for Registration: 4th July 2026 10.00 am
Website: https://www.zetwerk.com/
Location: Chennai (Tamil Nadu), Bangalore (Karnataka), Noida (Uttar Pradesh) &
Dharuhera (Haryana). Students must be open to all 4 locations.
Job Designation Offered: Intern, convertible to full-time as Post Graduate
Engineer Trainee after 10 months.
All interested and eligible students should register in the Neo PAT portal on
or before 4th July 2026 10.00 am. No manual registration or extension will be
entertained.\"\"\"
Correct output:
{
  "company_name": "Zetwerk Electronics",
  "role": "Intern",
  "opportunity_type": "internship_and_fulltime",
  "stipend": "15000",
  "package": "5 LPA fixed + 1 Lakh retention bonus (if converted to full-time)",
  "location": "Chennai, Bangalore, Noida, Dharuhera (open to all 4 locations)",
  "eligibility": "80% or 8.0 CGPA in X/XII; 70% or 7.0 CGPA in pursuing degree; no arrears",
  "cgpa_requirement": "7.0",
  "eligible_branches": [
    "M.Tech - Control and Automation", "M.Tech - Mechatronics",
    "M.Tech - Power Electronics & Drives", "M.Tech - Automotive Electronics",
    "M.Tech - Smart Manufacturing"
  ],
  "registration_deadline": "2026-07-04T10:00",
  "interview_date": null,
  "oa_date": null,
  "registration_link": null,
  "hiring_process": null,
  "important_notes": [
    "No manual registration or extension will be entertained",
    "Date of visit will be announced later"
  ],
  "update_type": "new_opportunity",
  "current_status": "OPEN",
  "action_required": "Register on the Neo PAT portal by 4 July 2026, 10:00 AM",
  "degree_level": "MTECH",
  "confidence": 0.9
}
(Note: "Website: https://www.zetwerk.com/" is the company's site, not a
registration link, so registration_link is null here too.)
"""


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
- These emails are Indian placement-cell correspondence: any numeric date is day-month-year,
  NOT month-day-year. "03/06/2026" and "3-06-2026" both mean 3 June 2026 — never read them as
  March 6. "1-07-2026" means 1 July 2026, never January 7. This only applies to ambiguous
  numeric dates; written month names ("17 June 2026") are already unambiguous.
- registration_deadline, oa_date, and interview_date are three DIFFERENT events — only fill
  in the ones the email actually states, and leave the others null:
  - registration_deadline: the last date/time to apply or register (look for "last date for
    registration", "apply by", "registration closes"). Not a visit date, not an OA or
    interview date.
  - oa_date: when an online assessment / online test / coding test happens (look for "online
    test", "OA scheduled", "assessment"). A test conducted virtually still counts as oa_date.
  - interview_date: when an interview / selection round / HR round / group discussion happens
    (look for "interview", "next round of selection process", "PPT", "group discussion",
    "HR round"). A "selection process" round is interview_date, not oa_date, unless the email
    explicitly calls it an online test/assessment.
- If the email includes a "Received: <ISO timestamp>" line, treat that as the date/time
  the email was received and resolve any relative date reference in the body (e.g.,
  "this Friday", "tomorrow", "in 3 days", "next Monday", "by EOD today") against it before
  converting to ISO 8601. Without a "Received:" line, do not guess an anchor date; if a
  relative date cannot be resolved, use null for that field.
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
{FEW_SHOT_EXAMPLES}
Now extract from this email:
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


def _extract_result_from_response(response: Any) -> dict[str, Any]:
    """Build the extraction dict from a Gemini response.

    Prefers the SDK's schema-validated ``response.parsed`` (populated because
    ``_generate_content`` passes ``response_schema=PlacementExtraction``),
    which already ran the model's own field validators via
    ``PlacementExtraction.model_validate_json``. Falls back to the legacy
    manual JSON parse/repair path when ``parsed`` isn't a valid
    PlacementExtraction (e.g. an injected fake model in tests that only sets
    ``.text``, or a response the SDK could not coerce to the schema).
    """
    parsed_model = getattr(response, "parsed", None)
    if isinstance(parsed_model, PlacementExtraction):
        return parsed_model.model_dump()

    raw_text = _response_text(response)
    parsed = parse_json_response(raw_text)
    return validate_extraction_result(parsed)


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
