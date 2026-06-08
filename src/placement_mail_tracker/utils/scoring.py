"""Priority scoring logic for Placement Opportunities."""

import logging
from datetime import datetime, timedelta

from placement_mail_tracker.config.user_profile import UserProfile
from placement_mail_tracker.extraction.eligibility import evaluate_eligibility
from placement_mail_tracker.utils.time import parse_datetime_flexible

logger = logging.getLogger(__name__)

def compute_priority(opportunity: dict, profile: UserProfile) -> str:
    """
    Compute priority of an opportunity (HIGH, MEDIUM, LOW) based on:
    - Eligibility
    - Proximity to deadline/events
    - Status progression
    """
    # 1. Eligibility Check
    # If not already evaluated, evaluate it.
    eligibility_status = opportunity.get("eligibility_status")
    if not eligibility_status or eligibility_status == "MANUAL_REVIEW":
        eligibility_status = evaluate_eligibility(opportunity, profile)
        
    if "NOT_ELIGIBLE" in eligibility_status:
        return "LOW"
        
    # 2. Status Check
    status = opportunity.get("current_status", "OPEN")
    high_priority_statuses = {"SHORTLISTED", "OA", "INTERVIEW", "HR", "SELECTED", "OFFER_RECEIVED"}
    if status in high_priority_statuses:
        return "HIGH"
        
    if status in {"REJECTED", "WITHDRAWN", "EXPIRED", "COMPLETED"}:
        return "LOW"

    # 3. Time Proximity Check
    now = datetime.now()
    
    # Check deadline
    deadline_str = opportunity.get("deadline")
    if deadline_str:
        deadline_dt = parse_datetime_flexible(deadline_str)
        if deadline_dt:
            time_left = deadline_dt - now
            if timedelta(0) < time_left <= timedelta(hours=48):
                return "HIGH"
            
    # Check next event
    next_event_str = opportunity.get("next_event_date")
    if next_event_str:
        next_event_dt = parse_datetime_flexible(next_event_str)
        if next_event_dt:
            time_left = next_event_dt - now
            if timedelta(0) < time_left <= timedelta(hours=48):
                return "HIGH"
                
    # 4. Default Fallback
    return "MEDIUM"
