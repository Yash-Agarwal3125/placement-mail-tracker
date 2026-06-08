"""User Profile Configuration for Placement Mail Tracker."""

import json
from pathlib import Path

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    """User profile for eligibility filtering."""
    
    degree: str = Field(..., description="E.g., B.Tech, M.Tech, MBA")
    branch: str = Field(..., description="E.g., AI & ML, Computer Science")
    campus: str = Field(..., description="E.g., Vellore, Chennai")
    graduation_year: int = Field(..., description="E.g., 2027")
    cgpa: float = Field(..., description="E.g., 8.7")

    @classmethod
    def load(cls, filepath: str = "config/user_profile.json") -> "UserProfile":
        """Load user profile from JSON file."""
        path = Path(filepath)
        if not path.exists():
            # Fallback default if not found
            return cls(
                degree="B.Tech",
                branch="Computer Science",
                campus="Vellore",
                graduation_year=2027,
                cgpa=8.0
            )
            
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return cls(**data)
