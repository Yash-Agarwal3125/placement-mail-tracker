"""Placeholder email filtering helpers."""

PLACEMENT_KEYWORDS = (
    "placement",
    "internship",
    "campus hiring",
    "recruitment",
    "job opening",
    "pre-placement",
)


def is_placement_related(subject: str, body: str = "") -> bool:
    """Return True when an email looks placement or internship related."""
    text = f"{subject} {body}".lower()
    return any(keyword in text for keyword in PLACEMENT_KEYWORDS)
