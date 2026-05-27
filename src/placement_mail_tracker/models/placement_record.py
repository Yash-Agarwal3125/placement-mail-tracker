"""Placement record data model."""

from dataclasses import dataclass


@dataclass(slots=True)
class PlacementRecord:
    """Structured placement or internship information."""

    gmail_message_id: str
    subject: str
    sender: str | None = None
    received_at: str | None = None
    category: str | None = None
    company_name: str | None = None
    role_title: str | None = None
    application_deadline: str | None = None
    source_url: str | None = None
    raw_snippet: str | None = None
