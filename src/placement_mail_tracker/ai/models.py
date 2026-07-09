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
    degree_level: str | None = None
    confidence: float | None = Field(
        default=None,
        description=(
            "The model's own self-assessed confidence (0.0-1.0) that this "
            "extraction as a whole is fully correct and nothing was guessed. "
            "Self-reported, not computed from a second call."
        ),
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
        "degree_level",
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

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: Any) -> float | None:
        """Coerce and clamp the model's self-reported confidence.

        Fail-soft by design (matches the rest of this model): an
        out-of-range or unparseable confidence value is clamped/dropped
        rather than raising, since ``confidence`` only ever feeds an
        informational review flag (extraction/validation.py) and must
        never itself cause a whole extraction to be rejected.
        """
        if value is None or isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, numeric))


def empty_extraction_payload() -> dict[str, Any]:
    """Return a complete empty extraction dictionary."""
    return PlacementExtraction().model_dump()
