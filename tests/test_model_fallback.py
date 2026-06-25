from unittest.mock import MagicMock

import pytest

from placement_mail_tracker.ai.gemini_extractor import (
    GeminiExtractionError,
    GeminiPlacementExtractor,
)
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

    total_models = 1 + len(extractor.settings.gemini_fallback_models)
    assert extractor._generate_content.call_count == total_models * extractor.max_retries
