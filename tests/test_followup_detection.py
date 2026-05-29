"""Phase 5 & 6: Follow-up Detection and Normalization Tests."""

import json
from placement_mail_tracker.utils.deduplication import normalize_company

def test_company_normalization():
    assert normalize_company("TATA MOTORS") == "Tata Motors"
    assert normalize_company("Tata Motors") == "Tata Motors"
    assert normalize_company("Tata motors ltd") == "Tata Motors"
    assert normalize_company("Tata Motors Ltd.") == "Tata Motors"

def test_thread_followup_detection(db_manager):
    # Email 1: OA
    opp1 = {"company_name": "Tata Motors", "role": "GET", "current_status": "OA"}
    id1, created1 = db_manager.insert_or_update_opportunity(opp1, source_email_id="msg1", source_thread_id="thread_tata")
    
    assert created1 is True
    
    # Email 2: Shortlist
    opp2 = {"company_name": "Tata Motors", "role": "GET", "current_status": "SHORTLISTED"}
    id2, created2 = db_manager.insert_or_update_opportunity(opp2, source_email_id="msg2", source_thread_id="thread_tata")
    
    assert created2 is False
    assert id1 == id2
    
    # Email 3: Interview
    opp3 = {"company_name": "Tata Motors", "role": "GET", "current_status": "INTERVIEW"}
    id3, created3 = db_manager.insert_or_update_opportunity(opp3, source_email_id="msg3", source_thread_id="thread_tata")
    
    assert created3 is False
    
    # Verify status history
    record = db_manager.fetch_opportunity_by_id(id1)
    history = record["status_history"]
    assert history == ["OA", "SHORTLISTED", "INTERVIEW"]

def test_separate_drives(db_manager):
    # Same company, different role/thread -> separate drives
    opp1 = {"company_name": "Tata Motors", "role": "GET"}
    id1, created1 = db_manager.insert_or_update_opportunity(opp1, source_thread_id="thread1")
    
    opp2 = {"company_name": "Tata Motors", "role": "Software Engineer"}
    id2, created2 = db_manager.insert_or_update_opportunity(opp2, source_thread_id="thread2")
    
    assert created2 is True
    assert id1 != id2
