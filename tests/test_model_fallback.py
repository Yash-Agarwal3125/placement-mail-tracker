import pytest
import json
from unittest.mock import MagicMock, patch
from google.genai import errors as genai_errors
from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor, GeminiExtractionError
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.ai.models import PlacementExtraction

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
    
    # We will mock the `_generate_content` directly to simulate model failures
    mock_responses = []
    
    # 1st call: gemini-2.5-flash fails
    def generate_content_side_effect(prompt, model_name):
        if model_name == "gemini-2.5-flash":
            raise ValueError("Rate limit exceeded")
        elif model_name == "gemini-2.0-flash":
            # Success!
            mock_response = MagicMock()
            mock_response.text = '{"company_name": "Google", "role": "SWE"}'
            return mock_response
        else:
            raise Exception("Should not reach here")
            
    extractor._generate_content = MagicMock(side_effect=generate_content_side_effect)
    
    result = extractor.extract_from_text("Test email")
    
    assert result["company_name"] == "Google"
    assert result["role"] == "SWE"
    assert extractor._generate_content.call_count == 4
    
def test_all_models_fail(test_settings):
    extractor = GeminiPlacementExtractor(test_settings)
    
    # All models fail
    def generate_content_side_effect(prompt, model_name):
        raise ValueError("Service unavailable")
            
    extractor._generate_content = MagicMock(side_effect=generate_content_side_effect)
    
    with pytest.raises(GeminiExtractionError):
        extractor.extract_from_text("Test email")
        
    assert extractor._generate_content.call_count == 9
