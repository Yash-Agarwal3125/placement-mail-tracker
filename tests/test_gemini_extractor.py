"""Tests for Gemini placement extraction helpers."""

from dataclasses import dataclass

import pytest

from placement_mail_tracker.ai.gemini_extractor import (
    EXTRACTION_FIELDS,
    GeminiPlacementExtractor,
    clean_email_content,
    clean_model_response,
    parse_json_response,
    validate_extraction_result,
)
from placement_mail_tracker.ai.models import PlacementExtraction
from placement_mail_tracker.config.settings import Settings


@dataclass
class FakeResponse:
    text: str


class FakeModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def generate_content(self, prompt: str) -> FakeResponse:
        self.calls += 1
        return FakeResponse(self.responses.pop(0))


def test_clean_email_content_removes_signature_and_disclaimer() -> None:
    cleaned = clean_email_content(
        subject="Campus Drive",
        sender="CDC <cdc@example.edu>",
        body=(
            "Company: ExampleTech\n"
            "Role: SDE Intern\n\n"
            "Regards,\n"
            "Placement Team\n\n"
            "This email and any attachments are confidential."
        ),
    )

    assert "Company: ExampleTech" in cleaned
    assert "Regards" not in cleaned
    assert "confidential" not in cleaned.lower()


def test_parse_json_response_handles_markdown_fence() -> None:
    parsed = parse_json_response(
        """```json
        {"company_name": "ExampleTech", "role": "SDE Intern"}
        ```"""
    )

    assert parsed["company_name"] == "ExampleTech"
    assert parsed["role"] == "SDE Intern"


def test_validate_extraction_result_returns_complete_schema() -> None:
    result = validate_extraction_result(
        {
            "company_name": "ExampleTech",
            "eligible_branches": "CSE, IT; ECE",
            "hiring_process": ["OA", "Interview"],
            "extra_field": "ignored",
        }
    )

    assert set(result) == set(EXTRACTION_FIELDS)
    assert result["company_name"] == "ExampleTech"
    assert result["eligible_branches"] == ["CSE", "IT", "ECE"]
    assert result["hiring_process"] == ["OA", "Interview"]
    assert result["role"] is None


def test_extractor_retries_after_invalid_json() -> None:
    settings = Settings(GEMINI_API_KEY="fake-key")
    model = FakeModel(
        [
            "not json",
            (
                '{"company_name": "ExampleTech", "role": "Backend Intern", '
                '"update_type": "new_opportunity"}'
            ),
        ]
    )
    extractor = GeminiPlacementExtractor(
        settings,
        model=model,
        max_retries=2,
        retry_delay_seconds=0,
    )

    result = extractor.extract_from_text("Company: ExampleTech\nRole: Backend Intern")

    assert model.calls == 2
    assert result["company_name"] == "ExampleTech"
    assert result["role"] == "Backend Intern"
    assert result["update_type"] == "new_opportunity"


def test_parse_json_response_rejects_non_object_json() -> None:
    with pytest.raises(ValueError):
        parse_json_response('["not", "an", "object"]')


def test_parse_json_response_repairs_trailing_comma() -> None:
    parsed = parse_json_response(
        """
        Here is the JSON:
        {
            "company_name": "ExampleTech",
            "role": "SDE Intern",
        }
        """
    )

    assert parsed["company_name"] == "ExampleTech"
    assert parsed["role"] == "SDE Intern"


def test_clean_model_response_removes_code_fence() -> None:
    assert clean_model_response('```json\n{"company_name": "A"}\n```') == '{"company_name": "A"}'


def test_pydantic_model_normalizes_list_fields() -> None:
    extraction = PlacementExtraction.model_validate(
        {
            "eligible_branches": "CSE, IT; ECE",
            "important_notes": ["Carry ID card", ""],
        }
    )

    assert extraction.eligible_branches == ["CSE", "IT", "ECE"]
    assert extraction.important_notes == ["Carry ID card"]


def test_legacy_field_aliases_are_supported() -> None:
    result = validate_extraction_result(
        {
            "internship_or_fulltime": "internship",
            "branches_allowed": ["CSE"],
            "deadline": "2026-06-01",
            "work_location": "Remote",
        }
    )

    assert result["opportunity_type"] == "internship"
    assert result["eligible_branches"] == ["CSE"]
    assert result["registration_deadline"] == "2026-06-01"
    assert result["location"] == "Remote"
