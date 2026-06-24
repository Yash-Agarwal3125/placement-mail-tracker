"""Eligibility Filtering Engine (Phases 2, 3, 6)."""

import json
import logging
import re

from rapidfuzz import fuzz

from placement_mail_tracker.config.user_profile import UserProfile

logger = logging.getLogger(__name__)

IT_DOMAIN_BRANCHES = [
    "ai & ml",
    "aiml",
    "artificial intelligence",
    "machine learning",
    "computer science",
    "computer science and engineering",
    "data science",
    "information technology",
    "cse",
    "it",
    "cs",
]

NON_IT_BRANCHES = [
    "mechanical",
    "civil",
    "chemical",
    "production",
    "electrical",
    "electronics",
]

NON_BTECH_DEGREES = [
    "mba", "m.tech", "mtech", "mca", "m.sc", "msc", "b.sc", "bsc", "b.com", "bcom", "phd"
]

# Text patterns that strongly signal an M.Tech-only opportunity
_MTECH_SIGNALS = re.compile(
    r"\bm\.?\s*tech\b|\bmaster\s+of\s+technology\b",
    re.IGNORECASE,
)


def evaluate_eligibility(opp_data: dict, profile: UserProfile) -> str:
    """Evaluate if the opportunity matches the user profile."""
    extracted_degree = (opp_data.get("degree_level") or "").upper()
    if extracted_degree == "MTECH":
        user_deg = profile.degree.lower().replace(".", "").replace(" ", "")
        if user_deg in ("btech", "be"):
            return "NOT_ELIGIBLE_DEGREE"

    eligibility_text = str(opp_data.get("eligibility") or "").lower()
    branches_raw = opp_data.get("branches_allowed") or ""
    branches_allowed = (
        " ".join(branches_raw) if isinstance(branches_raw, list)
        else str(branches_raw)
    ).lower()
    cgpa_req_str = str(opp_data.get("cgpa_requirement") or "").lower()

    if not eligibility_text and not branches_allowed and not cgpa_req_str:
        return "MANUAL_REVIEW"

    combined_text = f"{eligibility_text} {branches_allowed}"

    # M.Tech signal in eligibility/branch text even when degree_level is UNKNOWN
    if _MTECH_SIGNALS.search(combined_text):
        has_btech_signal = re.search(r"\bb\.?\s*tech\b|\bb\.e\b", combined_text, re.IGNORECASE)
        if not has_btech_signal:
            user_deg = profile.degree.lower().replace(".", "").replace(" ", "")
            if user_deg in ("btech", "be"):
                logger.info("[ELIGIBILITY] M.Tech signal in text; user is B.Tech — filtering")
                return "NOT_ELIGIBLE_DEGREE"

    if profile.degree.lower() in ("b.tech", "btech"):
        has_btech = bool(
            re.search(r"\bb\.?\s*tech\b|\bb\.e\b|\bb\.eng\b", combined_text, re.IGNORECASE)
        )
        has_other_degree = any(deg in combined_text for deg in NON_BTECH_DEGREES)
        if has_other_degree and not has_btech:
            logger.info("[ELIGIBILITY] Opportunity filtered - Not B.Tech")
            return "NOT_ELIGIBLE_DEGREE"

    if any(branch in combined_text for branch in NON_IT_BRANCHES):
        has_it_branch = any(it_branch in combined_text for it_branch in IT_DOMAIN_BRANCHES)
        if not has_it_branch:
            highest_score = max(
                fuzz.partial_ratio(it_branch, combined_text) for it_branch in IT_DOMAIN_BRANCHES
            )
            if highest_score < 80:
                logger.info("[ELIGIBILITY] Opportunity filtered - Branch mismatch")
                return "NOT_ELIGIBLE_BRANCH"

    if cgpa_req_str:
        floats = re.findall(r"(\d+\.\d+|\d+)", cgpa_req_str)
        if floats:
            try:
                required_cgpa = float(floats[0])
                if 4.0 <= required_cgpa <= 10.0:
                    if profile.cgpa < required_cgpa:
                        logger.info(
                            "[ELIGIBILITY] Opportunity filtered - CGPA %.1f < %.1f",
                            profile.cgpa, required_cgpa,
                        )
                        return "NOT_ELIGIBLE_CGPA"
            except ValueError:
                pass

    return "ELIGIBLE"


def format_eligibility_string(opp_data: dict) -> str:
    """Return a human-readable eligibility string like 'B.Tech - CSE, AI&ML'.

    Used for the Eligibility column in the sheets instead of the raw enum value.
    Returns '' when no degree or branch information is available.
    """
    degree_level = (opp_data.get("degree_level") or "UNKNOWN").upper()
    degree_label = {"BTECH": "B.Tech", "MTECH": "M.Tech", "ANY": "Any"}.get(degree_level, "")

    branches_raw = opp_data.get("branches_allowed") or []
    if isinstance(branches_raw, str):
        try:
            parsed = json.loads(branches_raw)
            branches_raw = parsed if isinstance(parsed, list) else [branches_raw]
        except (json.JSONDecodeError, ValueError):
            branches_raw = [branches_raw] if branches_raw.strip() else []

    clean_branches = [
        b.strip() for b in branches_raw
        if b.strip() and b.strip().lower() not in ("", "[]", "null")
    ]

    if not degree_label and not clean_branches:
        return ""

    if not clean_branches:
        return degree_label

    branch_str = ", ".join(clean_branches)
    if degree_label:
        return f"{degree_label} - {branch_str}"
    return branch_str
