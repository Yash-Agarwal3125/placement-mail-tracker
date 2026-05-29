"""Phase 4: Gemini Extraction Tests."""

import pytest
from unittest.mock import MagicMock, patch
from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor
from placement_mail_tracker.config.settings import Settings

@patch('google.genai.Client')
def test_gemini_full_extraction(mock_client):
    mock_response = MagicMock()
    mock_response.text = """{
        "company_name": "Infosys",
        "role": "Systems Engineer",
        "opportunity_type": "full_time",
        "package": "3.6 LPA",
        "update_type": "new_opportunity",
        "current_status": "NEW",
        "action_required": "Apply on portal"
    }"""
    mock_client.return_value.models.generate_content.return_value = mock_response
    
    extractor = GeminiPlacementExtractor(Settings(GEMINI_API_KEY="test"))
    result = extractor.extract_from_email({"subject": "Infosys Hiring"})
    
    assert result["company_name"] == "Infosys"
    assert result["role"] == "Systems Engineer"
    assert result["current_status"] == "NEW"
    assert result["action_required"] == "Apply on portal"

@patch('google.genai.Client')
def test_gemini_missing_role(mock_client):
    mock_response = MagicMock()
    mock_response.text = """{
        "company_name": "TCS",
        "role": null,
        "opportunity_type": "full_time",
        "update_type": "oa_update",
        "current_status": "OA"
    }"""
    mock_client.return_value.models.generate_content.return_value = mock_response
    
    extractor = GeminiPlacementExtractor(Settings(GEMINI_API_KEY="test"))
    result = extractor.extract_from_email({"subject": "TCS OA"})
    
    assert result["company_name"] == "TCS"
    assert result["role"] is None
    assert result["current_status"] == "OA"
