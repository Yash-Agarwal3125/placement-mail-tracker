"""User Profile Configuration for Placement Mail Tracker."""

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class UserProfile(BaseModel):
    """User profile for eligibility filtering and roster identity matching."""

    degree: str = Field(..., description="E.g., B.Tech, M.Tech, MBA")
    branch: str = Field(..., description="E.g., AI & ML, Computer Science")
    campus: str = Field(..., description="E.g., Vellore, Chennai")
    graduation_year: int = Field(..., description="E.g., 2027")
    cgpa: float = Field(..., description="E.g., 8.7")

    # Identity fields (docs/design/15 §3.1) — used only for roster/shortlist
    # matching, never for eligibility. Left blank means roster verification
    # must refuse to run (see is_identity_verified below).
    full_name: str = Field(default="", description="As printed on rosters")
    registration_no: str = Field(default="", description="E.g. 23BAI1234 — primary match key")
    name_aliases: list[str] = Field(default_factory=list)
    codenames: list[str] = Field(default_factory=list)

    # True only when this profile is the hardcoded fallback, never when
    # loaded from a real (even incomplete) config/user_profile.json.
    is_default: bool = Field(default=False, exclude=True)

    def is_identity_verified(self) -> bool:
        """Whether this profile is safe to use for roster/shortlist matching.

        Doc 15 §3.1: matching against a fallback or registration-number-less
        profile could assert a shortlist verdict about the wrong person. That
        is worse than not matching at all, so identity matching must refuse
        outright rather than degrade gracefully.
        """
        return not self.is_default and bool(self.registration_no.strip())

    @classmethod
    def _default(cls) -> "UserProfile":
        return cls(
            degree="B.Tech",
            branch="Computer Science",
            campus="Vellore",
            graduation_year=2027,
            cgpa=8.0,
            is_default=True,
        )

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
                "Eligibility filtering may be wrong, and roster/shortlist "
                "matching is disabled — create %s with your real degree, "
                "branch, campus, graduation_year, cgpa, full_name and "
                "registration_no.",
                path,
                path,
            )
            return cls._default()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except Exception as error:
            logger.error(
                "Could not parse user profile %s (%s); using default profile",
                path,
                error,
            )
            return cls._default()
