"""Phase 7: Database Tests."""

def test_drive_id_generation(db_manager):
    opp1 = {"company_name": "Google", "role": "SWE"}
    db_manager.insert_or_update_opportunity(opp1)
    
    opp2 = {"company_name": "Google", "role": "PM"}
    db_manager.insert_or_update_opportunity(opp2)
    
    active = db_manager.get_active_opportunities()
    assert len(active) == 2
    drives = sorted([o["drive_id"] for o in active])
    assert "GOOGLE" in drives[0]
    assert drives[0].endswith("_01")
    assert drives[1].endswith("_02")
    
def test_retry_queue_logging(db_manager):
    db_manager.log_processed_email(
        gmail_message_id="msg-err",
        subject="Failed Email",
        processed_status="PENDING_EXTRACTION",
        error_message="503 Unavailable"
    )
    row = db_manager.connection.execute("SELECT * FROM processed_emails WHERE gmail_message_id='msg-err'").fetchone()
    assert row["processed_status"] == "PENDING_EXTRACTION"
