from unittest.mock import MagicMock, patch

import pytest

from placement_mail_tracker.ai.gemini_extractor import (
    GeminiExtractionError,
    GeminiPlacementExtractor,
    GeminiQuotaExhaustedError,
)
from placement_mail_tracker.ai.models import PlacementExtraction
from placement_mail_tracker.config.settings import Settings


@pytest.fixture
def test_settings():
    settings = Settings(
        app_env="testing",
        gemini_api_key="fake-key",
        gemini_model="gemini-2.5-flash",
        gemini_fallback_models=["gemini-2.0-flash", "gemini-2.0-flash-lite"],
        gemini_max_retries=1, # Keep small for tests
        gemini_retry_delay_seconds=0.01
    )
    return settings

def test_successful_fallback(test_settings):
    extractor = GeminiPlacementExtractor(test_settings)
    primary_model = extractor.settings.gemini_model

    # Primary model always fails; any fallback model succeeds.
    def generate_content_side_effect(prompt, model_name):
        if model_name == primary_model:
            raise ValueError("Rate limit exceeded")
        mock_response = MagicMock()
        mock_response.text = '{"company_name": "Google", "role": "SWE"}'
        return mock_response

    extractor._generate_content = MagicMock(side_effect=generate_content_side_effect)

    result = extractor.extract_from_text("Test email")

    assert result["company_name"] == "Google"
    assert result["role"] == "SWE"
    # primary model is retried max_retries times, then first fallback succeeds once
    assert extractor._generate_content.call_count == extractor.max_retries + 1


def test_all_models_fail(test_settings):
    extractor = GeminiPlacementExtractor(test_settings)

    extractor._generate_content = MagicMock(side_effect=ValueError("Service unavailable"))

    with pytest.raises(GeminiExtractionError):
        extractor.extract_from_text("Test email")

    # Quota-aware cap: even though 2 fallback models are configured (3 total),
    # at most `max_models_to_try` (default 2) are ever attempted.
    total_models = min(
        1 + len(extractor.settings.gemini_fallback_models), extractor.max_models_to_try
    )
    assert extractor._generate_content.call_count == total_models * extractor.max_retries


def test_live_call_ceiling_is_capped(test_settings):
    """Even with every configured model failing, the live-call ceiling per
    email is max_models_to_try * max_retries (default 2 * 1 = 2), not
    "every configured fallback model" (previously up to 18 calls/email)."""
    extractor = GeminiPlacementExtractor(test_settings)
    assert extractor.max_models_to_try == 2

    extractor._generate_content = MagicMock(side_effect=ValueError("Service unavailable"))

    with pytest.raises(GeminiExtractionError):
        extractor.extract_from_text("Test email")

    assert extractor._generate_content.call_count == 2


# ---------------------------------------------------------------------------
# (c) Quota-aware deferral: GeminiQuotaExhaustedError is a distinct exception
# raised only when *every* attempted model hits a genuine per-day 429, not a
# generic failure or a per-minute throttle.
# ---------------------------------------------------------------------------


def _quota_error(model_label: str) -> ValueError:
    # Mirrors the real genai_errors.APIError string shape closely enough for
    # the "429" + "PerDay" substring check in _is_daily_quota_exhausted.
    return ValueError(
        f"429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric "
        f"'GenerateRequestsPerDayPerProjectPerModel' ({model_label})"
    )


def test_quota_exhaustion_raises_distinct_error(test_settings):
    """Every attempted model (capped to 2) hits a daily quota 429 -> the
    caller gets GeminiQuotaExhaustedError, not a generic extraction failure."""
    extractor = GeminiPlacementExtractor(test_settings)

    extractor._generate_content = MagicMock(
        side_effect=[_quota_error("primary"), _quota_error("fallback")]
    )

    with pytest.raises(GeminiQuotaExhaustedError):
        extractor.extract_from_text("Test email")

    assert extractor._generate_content.call_count == 2


def test_non_quota_failure_raises_generic_error(test_settings):
    """A non-quota failure on every attempted model stays a generic
    GeminiExtractionError so the existing rule-only fallback path is
    untouched."""
    extractor = GeminiPlacementExtractor(test_settings)

    extractor._generate_content = MagicMock(side_effect=ValueError("Service unavailable"))

    with pytest.raises(GeminiExtractionError) as exc_info:
        extractor.extract_from_text("Test email")

    assert not isinstance(exc_info.value, GeminiQuotaExhaustedError)


def test_partial_quota_failure_is_not_treated_as_exhausted(test_settings):
    """Quota is per-model: if only the primary hits a daily quota 429 but the
    fallback fails for an unrelated reason, that is a genuine extraction
    failure, not a "come back later" situation — should NOT raise
    GeminiQuotaExhaustedError."""
    extractor = GeminiPlacementExtractor(test_settings)

    extractor._generate_content = MagicMock(
        side_effect=[_quota_error("primary"), ValueError("malformed response")]
    )

    with pytest.raises(GeminiExtractionError) as exc_info:
        extractor.extract_from_text("Test email")

    assert not isinstance(exc_info.value, GeminiQuotaExhaustedError)


# ---------------------------------------------------------------------------
# (b) Structured output: response.parsed (schema-validated by the SDK) is
# preferred over manual JSON parsing when it is present and valid.
# ---------------------------------------------------------------------------


def test_structured_output_prefers_parsed_over_manual_json(test_settings):
    extractor = GeminiPlacementExtractor(test_settings)

    parsed = PlacementExtraction(company_name="Google", role="SWE")
    mock_response = MagicMock(parsed=parsed, text="this is not valid json at all {{{")
    extractor._generate_content = MagicMock(return_value=mock_response)

    result = extractor.extract_from_text("Test email")

    assert result["company_name"] == "Google"
    assert result["role"] == "SWE"
    assert extractor._generate_content.call_count == 1


def test_structured_output_falls_back_to_manual_json_when_unparsed(test_settings):
    """A MagicMock's auto-vivified `.parsed` attribute must not be mistaken
    for a real schema-validated result (regression guard for the isinstance
    check in _extract_result_from_response)."""
    extractor = GeminiPlacementExtractor(test_settings)

    # `.parsed` deliberately left unset: a bare MagicMock() would auto-vivify
    # `.parsed` as another MagicMock (truthy, but not a PlacementExtraction).
    mock_response = MagicMock(parsed=None, text='{"company_name": "Google", "role": "SWE"}')
    extractor._generate_content = MagicMock(return_value=mock_response)

    result = extractor.extract_from_text("Test email")

    assert result["company_name"] == "Google"
    assert result["role"] == "SWE"


def test_generate_content_passes_response_schema_to_sdk(test_settings):
    """The real (non-injected) client path must ask the SDK for structured
    output bound to PlacementExtraction, not just response_mime_type."""
    extractor = GeminiPlacementExtractor(test_settings)

    with patch("placement_mail_tracker.ai.gemini_extractor.genai.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.models.generate_content.return_value = MagicMock(
            parsed=PlacementExtraction(company_name="Google"), text="{}"
        )
        mock_client.models.list.return_value = []

        extractor._generate_content("prompt text", "gemini-2.5-flash")

        _, kwargs = mock_client.models.generate_content.call_args
        assert kwargs["config"]["response_schema"] is PlacementExtraction
        assert kwargs["config"]["response_mime_type"] == "application/json"


def test_generate_content_uses_zero_temperature(test_settings):
    """Deterministic extraction depends on temperature=0 — confirm it's still
    set (not a change; this test just pins the existing, correct value)."""
    extractor = GeminiPlacementExtractor(test_settings)

    with patch("placement_mail_tracker.ai.gemini_extractor.genai.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.models.generate_content.return_value = MagicMock(parsed=None, text="{}")
        mock_client.models.list.return_value = []

        extractor._generate_content("prompt text", "gemini-2.5-flash")

        _, kwargs = mock_client.models.generate_content.call_args
        assert kwargs["config"]["temperature"] == 0.0


# ---------------------------------------------------------------------------
# Prompt-and-validation workstream: confidence field, null-discipline, DMY
# convention, field definitions, and few-shot examples in the prompt.
# ---------------------------------------------------------------------------


class TestConfidenceField:
    def test_confidence_defaults_to_none(self):
        assert PlacementExtraction().confidence is None

    def test_confidence_accepts_valid_value(self):
        assert PlacementExtraction(confidence=0.75).confidence == 0.75

    def test_confidence_clamps_above_one(self):
        assert PlacementExtraction(confidence=1.5).confidence == 1.0

    def test_confidence_clamps_below_zero(self):
        assert PlacementExtraction(confidence=-0.3).confidence == 0.0

    def test_confidence_accepts_numeric_string(self):
        # The model sometimes emits numbers as strings; must not raise.
        assert PlacementExtraction.model_validate({"confidence": "0.4"}).confidence == 0.4

    def test_confidence_unparseable_value_becomes_none(self):
        # Fail-soft: a garbage confidence value must not blow up the whole
        # extraction's validation, since it's only an informational field.
        assert PlacementExtraction.model_validate({"confidence": "not a number"}).confidence is None

    def test_confidence_is_in_extraction_fields(self):
        from placement_mail_tracker.ai.gemini_extractor import EXTRACTION_FIELDS

        assert "confidence" in EXTRACTION_FIELDS


class TestPromptContent:
    def test_prompt_has_null_discipline_rule(self):
        from placement_mail_tracker.ai.gemini_extractor import build_extraction_prompt

        prompt = build_extraction_prompt("Subject: X\nSender: Y\n\nEmail Body:\nZ")
        assert "NEVER GUESS" in prompt

    def test_prompt_states_dmy_convention(self):
        from placement_mail_tracker.ai.gemini_extractor import build_extraction_prompt

        prompt = build_extraction_prompt("Subject: X\nSender: Y\n\nEmail Body:\nZ")
        assert "day-month-year" in prompt
        assert "1-07-2026" in prompt  # concrete disambiguation example

    def test_prompt_distinguishes_deadline_oa_interview(self):
        from placement_mail_tracker.ai.gemini_extractor import build_extraction_prompt

        prompt = build_extraction_prompt("Subject: X\nSender: Y\n\nEmail Body:\nZ")
        assert "registration_deadline: the last date" in prompt
        assert "oa_date: when an online assessment" in prompt
        assert "interview_date: when an interview" in prompt

    def test_prompt_includes_few_shot_examples(self):
        from placement_mail_tracker.ai.gemini_extractor import build_extraction_prompt

        prompt = build_extraction_prompt("Subject: X\nSender: Y\n\nEmail Body:\nZ")
        assert "Example 1" in prompt
        assert "Example 4" in prompt
        assert "Correct output:" in prompt

    def test_prompt_mentions_confidence_field(self):
        from placement_mail_tracker.ai.gemini_extractor import build_extraction_prompt

        prompt = build_extraction_prompt("Subject: X\nSender: Y\n\nEmail Body:\nZ")
        assert "confidence" in prompt

    def test_existing_relative_date_rule_still_present(self):
        # Regression guard: the input-completeness workstream's Received:/
        # relative-date rule must survive this prompt rewrite unmodified.
        from placement_mail_tracker.ai.gemini_extractor import build_extraction_prompt

        prompt = build_extraction_prompt("Subject: X\nSender: Y\n\nEmail Body:\nZ")
        assert "Received:" in prompt
        assert "relative date" in prompt.lower()
