"""User Profile Configuration for Placement Mail Tracker."""

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class UserProfile(BaseModel):
    """User profile for eligibility filtering."""

    degree: str = Field(..., description="E.g., B.Tech, M.Tech, MBA")
    branch: str = Field(..., description="E.g., AI & ML, Computer Science")
    campus: str = Field(..., description="E.g., Vellore, Chennai")
    graduation_year: int = Field(..., description="E.g., 2027")
    cgpa: float = Field(..., description="E.g., 8.7")

    @classmethod
    def load(cls, filepath: str = "config/user_profile.json") -> "UserProfile":
        """Load the user profile, falling back to a default (with a warning)."""
        path = Path(filepath)
        if not path.exists():
            # Eligibility filtering (Active vs Filtered tab) depends on this
            # profile. Falling back silently would mis-sort drives, so make the
            # fallback loud and tell the user how to fix it.
            logger.warning(
                "User profile not found at %s; using DEFAULT profile "
                "(B.Tech / Computer Science / Vellore / 2027 / CGPA 8.0). "
                "Eligibility filtering may be wrong — create %s with your real "
                "degree, branch, campus, graduation_year and cgpa.",
                path,
                path,
            )
            return cls(
                degree="B.Tech",
                branch="Computer Science",
                campus="Vellore",
                graduation_year=2027,
                cgpa=8.0,
            )

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except Exception as error:
            logger.error(
                "Could not parse user profile %s (%s); using default profile",
                path,
                error,
            )
            return cls(
                degree="B.Tech",
                branch="Computer Science",
                campus="Vellore",
                graduation_year=2027,
                cgpa=8.0,
            )
