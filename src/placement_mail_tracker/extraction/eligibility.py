"""Eligibility Filtering Engine (Phases 2, 3, 6)."""

import logging
import re

from rapidfuzz import fuzz

from placement_mail_tracker.config.user_profile import UserProfile

logger = logging.getLogger(__name__)

# Core allowed branches for IT domain (CS/IT/AI/DS)
# Used for fuzzy matching or explicit substring check
IT_DOMAIN_BRANCHES = [
    "ai & ml",
    "artificial intelligence",
    "machine learning",
    "computer science",
    "computer science and engineering",
    "data science",
    "information technology",
    "cse",
    "it",
    "cs"
]

# Specifically rejected branches if the opportunity is *only* for these
NON_IT_BRANCHES = [
    "mechanical",
    "civil",
    "chemical",
    "production",
    "electrical",
    "electronics" # Often grouped with IT, but prompt specifically mentions Mech/Civil/Chem/Prod
]

NON_BTECH_DEGREES = [
    "mba", "m.tech", "mtech", "mca", "m.sc", "msc", "b.sc", "bsc", "b.com", "bcom", "phd"
]

def evaluate_eligibility(opp_data: dict, profile: UserProfile) -> str:
    """Evaluate if the opportunity matches the user profile."""
    # Structured degree check takes precedence over text-based heuristics.
    extracted_degree = (opp_data.get("degree_level") or "").upper()
    if extracted_degree == "MTECH":
        user_deg = profile.degree.lower().replace(".", "").replace(" ", "")
        if user_deg in ("btech", "be"):
            return "NOT_ELIGIBLE_DEGREE"

    eligibility_text = str(opp_data.get("eligibility") or "").lower()
    branches_allowed = str(opp_data.get("branches_allowed") or "").lower()
    cgpa_req_str = str(opp_data.get("cgpa_requirement") or "").lower()
    
    # Check if there is absolutely no eligibility data
    if not eligibility_text and not branches_allowed and not cgpa_req_str:
        return "MANUAL_REVIEW"
        
    combined_text = f"{eligibility_text} {branches_allowed}"

    # Phase 2: Degree Filter
    if profile.degree.lower() == "b.tech" or profile.degree.lower() == "btech":
        # If it explicitly asks for other degrees and doesn't mention B.Tech
        has_btech = "b.tech" in combined_text or "btech" in combined_text or "b.e" in combined_text
        has_other_degree = any(deg in combined_text for deg in NON_BTECH_DEGREES)
        
        if has_other_degree and not has_btech:
            logger.info("[ELIGIBILITY] Opportunity filtered - Not B.Tech (Found %s)", combined_text)
            return "NOT_ELIGIBLE_DEGREE"
        elif has_btech:
            logger.info("[ELIGIBILITY] Degree matched")
            
    # Phase 3: Branch Filter
    # Check if it ONLY allows Mechanical, Civil, Chemical, Production
    if any(branch in combined_text for branch in NON_IT_BRANCHES):
        # Make sure it doesn't also allow CS/IT
        has_it_branch = any(it_branch in combined_text for it_branch in IT_DOMAIN_BRANCHES)
        # Using RapidFuzz for fuzzy matching if exact isn't found
        if not has_it_branch:
            # Let's do a fuzzy check against the text
            highest_score = 0
            for it_branch in IT_DOMAIN_BRANCHES:
                score = fuzz.partial_ratio(it_branch, combined_text)
                if score > highest_score:
                    highest_score = score
            
            # If fuzzy score is low, it's definitely not for our branch
            if highest_score < 80:
                logger.info("[ELIGIBILITY] Opportunity filtered - Branch mismatch")
                return "NOT_ELIGIBLE_BRANCH"
                
    # If there are explicit branches mentioned, check if ours is there
    if branches_allowed and len(branches_allowed) > 5:
        has_it_branch = any(it_branch in branches_allowed for it_branch in IT_DOMAIN_BRANCHES)
        if has_it_branch:
            logger.info("[ELIGIBILITY] Branch matched")
            
    # Phase 6: CGPA Check
    if cgpa_req_str:
        # Extract floats from cgpa_req_str (e.g. "7.5 CGPA" or "cgpa > 8.0")
        floats = re.findall(r"(\d+\.\d+|\d+)", cgpa_req_str)
        if floats:
            try:
                required_cgpa = float(floats[0])
                # Sanity check that it's a CGPA and not some other number like a year
                if 4.0 <= required_cgpa <= 10.0:
                    if profile.cgpa < required_cgpa:
                        logger.info(
                            "[ELIGIBILITY] Opportunity filtered - CGPA %.1f < %.1f", 
                            profile.cgpa, required_cgpa
                        )
                        return "NOT_ELIGIBLE_CGPA"
                    else:
                        logger.info("[ELIGIBILITY] CGPA check passed")
            except ValueError:
                pass

    return "ELIGIBLE"
