"""Dynamic trusted sender discovery and scoring for Placement Mail Tracker."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from placement_mail_tracker.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

# Keywords for institutional sender discovery and their weights
SENDER_KEYWORDS = {
    "career development centre": 55,
    "career development center": 55,
    "training and placement": 50,
    "placement office": 45,
    "career development": 40,
    "placement": 40,
    "placements": 40,
    "cdc": 40,
    "tpo": 35,
    "recruitment": 30,
    "campus hiring": 30,
    "campus drive": 30,
    "helpdesk": 20,
    "internship": 20,
}

# Strong institutional/placement subject keywords that boost discovery confidence
SUBJECT_DISCOVERY_BOOST = {
    "shortlist": 15,
    "interview schedule": 15,
    "online assessment": 15,
    "oa link": 15,
    "campus recruitment": 10,
    "placement drive": 10,
}


@dataclass
class TrustedSender:
    """Represents a discovered and verified institutional sender."""

    email: str
    display_name: str
    score: int
    matched_keywords: list[str]
    first_seen: str
    last_seen: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrustedSender:
        return cls(
            email=data["email"],
            display_name=data["display_name"],
            score=data["score"],
            matched_keywords=data["matched_keywords"],
            first_seen=data["first_seen"],
            last_seen=data["last_seen"],
        )


class TrustedSenderManager:
    """Manage dynamic discovery, persistence, and verification of trusted senders."""

    def __init__(
        self,
        storage_path: Path | None = None,
        *,
        trust_threshold: int = 50,
    ) -> None:
        if storage_path is None:
            self.storage_path = Path("data/trusted_senders.json")
        else:
            self.storage_path = storage_path

        self.trust_threshold = trust_threshold
        self.senders: dict[str, TrustedSender] = {}
        self.load_senders()

    def load_senders(self) -> None:
        """Load trusted senders from storage path."""
        if not self.storage_path.exists():
            self.senders = {}
            return

        try:
            content = self.storage_path.read_text(encoding="utf-8")
            loaded = json.loads(content)
            self.senders = {
                item["email"].lower().strip(): TrustedSender.from_dict(item)
                for item in loaded
                if "email" in item
            }
            logger.debug("Loaded %s trusted senders from %s", len(self.senders), self.storage_path)
        except Exception as error:
            logger.error("Failed to load trusted senders from %s: %s", self.storage_path, error)
            self.senders = {}

    def save_senders(self) -> None:
        """Save trusted senders to storage path securely."""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = [sender.to_dict() for sender in self.senders.values()]
            self.storage_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            logger.debug("Saved %s trusted senders to %s", len(self.senders), self.storage_path)
        except Exception as error:
            logger.error("Failed to save trusted senders to %s: %s", self.storage_path, error)

    def parse_from_header(self, from_header: str) -> tuple[str, str]:
        """Parse raw 'From' header to clean email and display name."""
        display_name, email = parseaddr(from_header)
        return email.lower().strip(), display_name.strip()

    def is_trusted(self, email: str) -> bool:
        """Check if an email address is verified as trusted."""
        clean_email = email.lower().strip()
        sender = self.senders.get(clean_email)
        return sender is not None and sender.score >= self.trust_threshold

    def calculate_sender_score(
        self,
        email: str,
        display_name: str,
        subject: str = "",
    ) -> tuple[int, list[str]]:
        """Evaluate a sender display name, email local part, and subject for placement signals."""
        email_clean = email.lower().strip()
        display_clean = display_name.lower()
        subject_clean = subject.lower()

        # Extract local-part of the email (e.g. cdc-office@college.edu -> cdc-office)
        local_part = email_clean.split("@")[0] if "@" in email_clean else email_clean

        score = 0
        matched_keywords: list[str] = []

        # 1. Match display name and email local part against signals
        for keyword, weight in SENDER_KEYWORDS.items():
            pattern = rf"\b{re.escape(keyword)}\b"
            
            # Check display name
            if re.search(pattern, display_clean):
                score += weight
                matched_keywords.append(f"display:{keyword}")
                
            # Check local part of email
            elif keyword in local_part:
                score += weight
                matched_keywords.append(f"email:{keyword}")

        # 2. Add temporary or discovery boost if subject strongly indicates placement action
        for term, boost in SUBJECT_DISCOVERY_BOOST.items():
            if term in subject_clean:
                score += boost
                matched_keywords.append(f"subject:{term}")

        # Deduplicate matched keywords
        matched_keywords = list(dict.fromkeys(matched_keywords))
        
        # Max out score at 100, min at 0
        final_score = max(0, min(score, 100))
        return final_score, matched_keywords

    def process_and_discover(
        self,
        from_header: str,
        subject: str = "",
    ) -> tuple[bool, int]:
        """Analyze a sender, discover if they are trusted, update historical listings, and save.

        Returns (is_trusted, score).
        """
        email, display_name = self.parse_from_header(from_header)
        if not email:
            return False, 0

        # Don't discover common spam/irrelevant standard addresses
        if any(domain in email for domain in {"medium.com", "quora.com", "substack.com", "udemy.com"}):
            return False, 0

        calculated_score, matched_keywords = self.calculate_sender_score(email, display_name, subject)
        now = utc_now_iso()

        existing = self.senders.get(email)

        if existing:
            # Update timestamps and potentially increase score if new keywords match
            existing.last_seen = now
            if calculated_score > existing.score:
                existing.score = calculated_score
                existing.matched_keywords = list(set(existing.matched_keywords + matched_keywords))
                logger.info("Updated trusted sender score for %s to %s", email, calculated_score)
            
            self.save_senders()
            return existing.score >= self.trust_threshold, existing.score

        # Discovered a brand new sender with positive placement signals!
        if calculated_score > 0:
            new_sender = TrustedSender(
                email=email,
                display_name=display_name or "Unknown Institutional Aliases",
                score=calculated_score,
                matched_keywords=matched_keywords,
                first_seen=now,
                last_seen=now,
            )
            self.senders[email] = new_sender
            logger.info("Discovered new institutional sender: %s (Score: %s)", email, calculated_score)
            self.save_senders()
            return calculated_score >= self.trust_threshold, calculated_score

        return False, calculated_score
