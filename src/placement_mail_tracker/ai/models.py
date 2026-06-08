"""Pydantic models for AI extraction responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlacementExtraction(BaseModel):
    """Strict structured data extracted from a placement email."""

    company_name: str | None = None
    role: str | None = None
    opportunity_type: str | None = None
    stipend: str | None = None
    package: str | None = None
    location: str | None = None
    eligibility: str | None = None
    cgpa_requirement: str | None = None
    eligible_branches: list[str] | None = Field(default=None)
    registration_deadline: str | None = None
    interview_date: str | None = None
    oa_date: str | None = None
    registration_link: str | None = None
    hiring_process: list[str] | None = Field(default=None)
    important_notes: list[str] | None = Field(default=None)
    update_type: str | None = None
    current_status: str | None = None
    action_required: str | None = Field(
        default=None,
        description="Action required by the user, if any.",
    )

    model_config = ConfigDict(extra="ignore")

    @field_validator("eligible_branches", "hiring_process", "important_notes", mode="before")
    @classmethod
    def normalize_list_fields(cls, value: Any) -> list[str] | None:
        """Normalize list-like fields from messy model output."""
        if value is None:
            return None

        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            return cleaned or None

        normalized = str(value).strip()
        if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
            return None

        separators = [",", ";", "\n"]
        values = [normalized]
        for separator in separators:
            values = [
                part.strip() for item in values for part in item.split(separator) if part.strip()
            ]

        return values or None

    @field_validator(
        "company_name",
        "role",
        "opportunity_type",
        "stipend",
        "package",
        "location",
        "eligibility",
        "cgpa_requirement",
        "registration_deadline",
        "interview_date",
        "oa_date",
        "registration_link",
        "update_type",
        "current_status",
        "action_required",
        mode="before",
    )
    @classmethod
    def normalize_scalar_fields(cls, value: Any) -> str | None:
        """Normalize scalar fields from model output."""
        if value is None:
            return None

        if isinstance(value, list):
            value = ", ".join(str(item).strip() for item in value if str(item).strip())

        normalized = str(value).strip()
        if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
            return None
        return normalized


def empty_extraction_payload() -> dict[str, Any]:
    """Return a complete empty extraction dictionary."""
    return PlacementExtraction().model_dump()
